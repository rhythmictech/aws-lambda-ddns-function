import json
import boto3
import re
import uuid
import time
import random
from datetime import datetime

import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logging.getLogger('botocore').setLevel(logging.CRITICAL)
logging.getLogger('urllib3').setLevel(logging.CRITICAL)

logger.info('Loading function ' + datetime.now().time().isoformat())
route53 = boto3.client('route53')
ec2 = boto3.resource('ec2')
compute = boto3.client('ec2')
dynamodb_client = boto3.client('dynamodb')
dynamodb_resource = boto3.resource('dynamodb')

def lambda_handler(event, context):
    """ Check to see whether a DynamoDB table already exists.  If not, create it.  This table is used to keep a record of
    instances that have been created along with their attributes.  This is necessary because when you terminate an instance
    its attributes are no longer available, so they have to be fetched from the table."""
    tables = dynamodb_client.list_tables()
    if 'DDNS' in tables['TableNames']:
        logger.debug('DynamoDB table already exists')
    else:
        logger.info('DynamoDB table does not exist, creating')
        create_table('DDNS')

    # Set variables
    # Get the state from the Event stream
    state = event['detail']['state']

    # Get the instance id, region, and tag collection
    instance_id = event['detail']['instance-id']
    region = event['region']
    table = dynamodb_resource.Table('DDNS')

    if state == 'running':
        time.sleep(60)
        instance = compute.describe_instances(InstanceIds=[instance_id])
        # Remove response metadata from the response
        instance.pop('ResponseMetadata')
        # Remove null values from the response.  You cannot save a dict/JSON document in DynamoDB if it contains null
        # values
        instance = remove_empty_from_dict(instance)
        instance_dump = json.dumps(instance,default=json_serial)
        instance_attributes = json.loads(instance_dump)
        table.put_item(
            Item={
                'InstanceId': instance_id,
                'InstanceAttributes': instance_attributes
            }
        )
    else:
        # Fetch item from DynamoDB
        instance = table.get_item(
        Key={
            'InstanceId': instance_id
        },
        AttributesToGet=[
            'InstanceAttributes'
            ]
        )
        instance = instance['Item']['InstanceAttributes']

    try:
        tags = instance['Reservations'][0]['Instances'][0]['Tags']
    except:
        tags = []
    # Get instance attributes
    private_ip = instance['Reservations'][0]['Instances'][0]['PrivateIpAddress']
    private_dns_name = instance['Reservations'][0]['Instances'][0]['PrivateDnsName']
    private_host_name = private_dns_name.split('.')[0]
    try:
        public_ip = instance['Reservations'][0]['Instances'][0]['PublicIpAddress']
        public_dns_name = instance['Reservations'][0]['Instances'][0]['PublicDnsName']
        public_host_name = public_dns_name.split('.')[0]
    except BaseException as e:
        logger.info('Instance has no public IP or host name')

    machine_name = get_instance_tag(tags, "MACHINE_NAME")

    # Get the subnet mask of the instance
    subnet_id = instance['Reservations'][0]['Instances'][0]['SubnetId']
    subnet = ec2.Subnet(subnet_id)
    cidr_block = subnet.cidr_block
    subnet_mask = int(cidr_block.split('/')[-1])

    reversed_ip_address = reverse_list(private_ip)
    reversed_domain_prefix = get_reversed_domain_prefix(subnet_mask, private_ip)
    reversed_domain_prefix = reverse_list(reversed_domain_prefix)

    # Set the reverse lookup zone
    reversed_lookup_zone = reversed_domain_prefix + 'in-addr.arpa.'
    logger.debug('The reverse lookup zone for this instance is:', reversed_lookup_zone)

    # Get VPC id
    vpc_id = instance['Reservations'][0]['Instances'][0]['VpcId']
    vpc = ec2.Vpc(vpc_id)

    # Are DNS Hostnames and DNS Support enabled?
    if is_dns_hostnames_enabled(vpc):
        logger.debug('DNS hostnames enabled for %s' % vpc_id)
    else:
        logger.warn('DNS hostnames disabled for %s.  You have to enable DNS hostnames to use Route 53 private hosted zones.' % vpc_id)
        exit()
    if is_dns_support_enabled(vpc):
        logger.debug('DNS support enabled for %s' % vpc_id)
    else:
        logger.warn('DNS support disabled for %s.  You have to enabled DNS support to use Route 53 private hosted zones.' % vpc_id)
        exit()

    # Create the public and private hosted zone collections.  These are collections of zones in Route 53.
    hosted_zones = route53.list_hosted_zones()
    private_hosted_zones = filter(lambda x: x['Config']['PrivateZone'] is True, hosted_zones['HostedZones'])
    private_hosted_zone_collection = map(lambda x: x['Name'], private_hosted_zones)
    public_hosted_zones = filter(lambda x: x['Config']['PrivateZone'] is False, hosted_zones['HostedZones'])
    public_hosted_zones_collection = map(lambda x: x['Name'], public_hosted_zones)

    # Check to see whether a reverse lookup zone for the instance already exists.  If it does, check to see whether
    # the reverse lookup zone is associated with the instance's VPC.  If it isn't create the association.  You don't
    # need to do this when you create the reverse lookup zone because the association is done automatically.
    if filter(lambda record: record['Name'] == reversed_lookup_zone, hosted_zones['HostedZones']):

        logger.info('Reverse lookup zone found: %s' % reversed_lookup_zone)
        reverse_lookup_zone_id = get_zone_id(reversed_lookup_zone)
        reverse_hosted_zone_properties = get_hosted_zone_properties(reverse_lookup_zone_id)
        if vpc_id in map(lambda x: x['VPCId'], reverse_hosted_zone_properties['VPCs']):
            logger.debug('Reverse lookup zone %s is associated with VPC %s' % (reverse_lookup_zone_id, vpc_id))
        else:
            logger.info('Associating zone %s with VPC %s' % (reverse_lookup_zone_id, vpc_id))
            try:
                associate_zone(reverse_lookup_zone_id, region, vpc_id)
            except BaseException as e:
                logger.exception()
    else:

        # create private hosted zone for reverse lookups
        if state == 'running':
            logger.info('No matching reverse lookup zone found, creating one')
            create_reverse_lookup_zone(instance, reversed_domain_prefix, region)
            reverse_lookup_zone_id = get_zone_id(reversed_lookup_zone)

    # Is there a DHCP option set?
    # Get DHCP option set configuration
    try:
        dhcp_options_id = vpc.dhcp_options_id
        dhcp_configurations = get_dhcp_configurations(dhcp_options_id)
    except BaseException as e:
        logger.exception('No DHCP option set assigned to this VPC, exiting\n')
        exit()

    # Look to see whether there's a DHCP option set assigned to the VPC.  If there is, use the value of the domain name
    # to create resource records in the appropriate Route 53 private hosted zone. This will also check to see whether
    # there's an association between the instance's VPC and the private hosted zone.  If there isn't, it will create it.
    for configuration in dhcp_configurations:
        if configuration[0] in private_hosted_zone_collection:
            private_hosted_zone_name = configuration[0]
            logger.info('Private zone found in DHCP option set: %s' % private_hosted_zone_name)
            # TODO need a way to prevent overlapping subdomains
            private_hosted_zone_id = get_zone_id(private_hosted_zone_name)
            private_hosted_zone_properties = get_hosted_zone_properties(private_hosted_zone_id)

            # use machine name if specified
            if machine_name is not None:
                private_host_name = machine_name
                private_dns_name = machine_name + '.' + private_hosted_zone_name

            # create A records and PTR records
            if state == 'running':
                if vpc_id in map(lambda x: x['VPCId'], private_hosted_zone_properties['VPCs']):
                    logger.debug('Private hosted zone %s is associated with VPC %s' % (private_hosted_zone_id, vpc_id))
                else:
                    logger.info('Associating zone %s with VPC %s' % (private_hosted_zone_id, vpc_id))
                    try:
                        associate_zone(private_hosted_zone_id, region,vpc_id)
                    except BaseException as e:
                        logger.exception('You cannot create an association with a VPC with an overlapping subdomain.\n')
                        exit()
                try:
                    create_resource_record(private_hosted_zone_id, private_host_name, private_hosted_zone_name, 'A', private_ip)
                    create_resource_record(reverse_lookup_zone_id, reversed_ip_address, 'in-addr.arpa', 'PTR', private_dns_name)
                except BaseException as e:
                    logger.exception()
            else:
                try:
                    delete_resource_record(private_hosted_zone_id, private_host_name, private_hosted_zone_name, 'A', private_ip)
                    delete_resource_record(reverse_lookup_zone_id, reversed_ip_address, 'in-addr.arpa', 'PTR', private_dns_name)
                except BaseException as e:
                    logger.exception()
        else:
            logger.info('No matching zone for %s' % configuration[0])
            
    # Clean up DynamoDB after deleting records
    if state != 'running':
        table.delete_item(
            Key={
                'InstanceId': instance_id
            }
        )
        
def create_table(table_name):
    dynamodb_client.create_table(
            TableName=table_name,
            AttributeDefinitions=[
                {
                    'AttributeName': 'InstanceId',
                    'AttributeType': 'S'
                },
            ],
            KeySchema=[
                {
                    'AttributeName': 'InstanceId',
                    'KeyType': 'HASH'
                },
            ],
            ProvisionedThroughput={
                'ReadCapacityUnits': 4,
                'WriteCapacityUnits': 4
            }
        )
    table = dynamodb_resource.Table(table_name)
    table.wait_until_exists()

def create_resource_record(zone_id, host_name, hosted_zone_name, type, value):
    """This function creates resource records in the hosted zone passed by the calling function."""
    logger.info('Updating %s record %s in zone %s ' % (type, host_name, hosted_zone_name))
    if host_name[-1] != '.':
        host_name = host_name + '.'
    route53.change_resource_record_sets(
                HostedZoneId=zone_id,
                ChangeBatch={
                    "Comment": "Updated by Lambda DDNS",
                    "Changes": [
                        {
                            "Action": "UPSERT",
                            "ResourceRecordSet": {
                                "Name": host_name + hosted_zone_name,
                                "Type": type,
                                "TTL": 60,
                                "ResourceRecords": [
                                    {
                                        "Value": value
                                    },
                                ]
                            }
                        },
                    ]
                }
            )

def delete_resource_record(zone_id, host_name, hosted_zone_name, type, value):
    """This function deletes resource records from the hosted zone passed by the calling function."""
    logger.info('Deleting %s record %s in zone %s' % (type, host_name, hosted_zone_name))
    if host_name[-1] != '.':
        host_name = host_name + '.'
    route53.change_resource_record_sets(
                HostedZoneId=zone_id,
                ChangeBatch={
                    "Comment": "Updated by Lambda DDNS",
                    "Changes": [
                        {
                            "Action": "DELETE",
                            "ResourceRecordSet": {
                                "Name": host_name + hosted_zone_name,
                                "Type": type,
                                "TTL": 60,
                                "ResourceRecords": [
                                    {
                                        "Value": value
                                    },
                                ]
                            }
                        },
                    ]
                }
            )
def get_zone_id(zone_name):
    """This function returns the zone id for the zone name that's passed into the function."""
    if zone_name[-1] != '.':
        zone_name = zone_name + '.'
    hosted_zones = route53.list_hosted_zones()
    x = filter(lambda record: record['Name'] == zone_name, hosted_zones['HostedZones'])
    try:
        zone_id_long = x[0]['Id']
        zone_id = str.split(str(zone_id_long),'/')[2]
        return zone_id
    except:
        return None

def is_valid_hostname(hostname):
    """This function checks to see whether the hostname entered into the zone and cname tags is a valid hostname."""
    if hostname is None or len(hostname) > 255:
        return False
    if hostname[-1] == ".":
        hostname = hostname[:-1]
    allowed = re.compile("(?!-)[A-Z\d-]{1,63}(?<!-)$", re.IGNORECASE)
    return all(allowed.match(x) for x in hostname.split("."))

def get_dhcp_configurations(dhcp_options_id):
    """This function returns the names of the zones/domains that are in the option set."""
    zone_names = []
    dhcp_options = ec2.DhcpOptions(dhcp_options_id)
    dhcp_configurations = dhcp_options.dhcp_configurations
    for configuration in dhcp_configurations:
        zone_names.append(map(lambda x: x['Value'] + '.', configuration['Values']))
    return zone_names

def reverse_list(list):
    """Reverses the order of the instance's IP address and helps construct the reverse lookup zone name."""
    if (re.search('\d{1,3}.\d{1,3}.\d{1,3}.\d{1,3}',list)) or (re.search('\d{1,3}.\d{1,3}.\d{1,3}\.',list)) or (re.search('\d{1,3}.\d{1,3}\.',list)) or (re.search('\d{1,3}\.',list)):
        list = str.split(str(list),'.')
        list = filter(None, list)
        list.reverse()
        reversed_list = ''
        for item in list:
            reversed_list = reversed_list + item + '.'
        return reversed_list
    else:
        logger.error('Not a valid ip')
        exit()

def get_reversed_domain_prefix(subnet_mask, private_ip):
    """Uses the mask to get the zone prefix for the reverse lookup zone"""
    if 32 >= subnet_mask >= 24:
        third_octet = re.search('\d{1,3}.\d{1,3}.\d{1,3}.',private_ip)
        return third_octet.group(0)
    elif 24 > subnet_mask >= 16:
        second_octet = re.search('\d{1,3}.\d{1,3}.', private_ip)
        return second_octet.group(0)
    else:
        first_octet = re.search('\d{1,3}.', private_ip)
        return first_octet.group(0)

def create_reverse_lookup_zone(instance, reversed_domain_prefix, region):
    """Creates the reverse lookup zone."""
    logger.info('Creating reverse lookup zone %s' % reversed_domain_prefix + 'in.addr.arpa.')
    route53.create_hosted_zone(
        Name = reversed_domain_prefix + 'in-addr.arpa.',
        VPC = {
            'VPCRegion':region,
            'VPCId': instance['Reservations'][0]['Instances'][0]['VpcId']
        },
        CallerReference=str(uuid.uuid1()),
        HostedZoneConfig={
            'Comment': 'Updated by Lambda DDNS',
        },
    )

def json_serial(obj):
    """JSON serializer for objects not serializable by default json code"""
    if isinstance(obj, datetime):
        serial = obj.isoformat()
        return serial
    raise TypeError ("Type not serializable")

def remove_empty_from_dict(d):
    """Removes empty keys from dictionary"""
    if type(d) is dict:
        return dict((k, remove_empty_from_dict(v)) for k, v in d.iteritems() if v and remove_empty_from_dict(v))
    elif type(d) is list:
        return [remove_empty_from_dict(v) for v in d if v and remove_empty_from_dict(v)]
    else:
        return d

def associate_zone(hosted_zone_id, region, vpc_id):
    """Associates private hosted zone with VPC"""
    route53.associate_vpc_with_hosted_zone(
        HostedZoneId=hosted_zone_id,
        VPC={
            'VPCRegion': region,
            'VPCId': vpc_id
        },
        Comment='Updated by Lambda DDNS'
    )

def is_dns_hostnames_enabled(vpc):
    dns_hostnames_enabled = vpc.describe_attribute(
    DryRun=False,
    Attribute='enableDnsHostnames'
)
    return dns_hostnames_enabled['EnableDnsHostnames']['Value']

def is_dns_support_enabled(vpc):
    dns_support_enabled = vpc.describe_attribute(
    DryRun=False,
    Attribute='enableDnsSupport'
)
    return dns_support_enabled['EnableDnsSupport']['Value']

def get_hosted_zone_properties(zone_id):
    hosted_zone_properties = route53.get_hosted_zone(Id=zone_id)
    hosted_zone_properties.pop('ResponseMetadata')
    return hosted_zone_properties

def get_instance_tag(tags, tag_name):
  for tag in tags:
    if tag_name == tag.get('Key'):
      return tag.get('Value')

  return None
