# EC2 Instance Dynamic DNS registration

## Introduction
When using central authentication and other nice things in life, it can be valuable
to have consistent forward/reverse DNS across various AWS accounts, but letting
instances register themselves in Route 53 is generally not ideal. Using things like
Puppet or Ansible to do the same typically results in stale records over time, plus
takes what should be a provisioning task and converts it into a configuration task.

This project uses CloudWatch Events and a Lambda function to detect instance
creation/deletion and manage corresponding Route 53 records on your behalf. This
is all driven through CloudFormation, which makes it easy to set up.

The forward hostname is computed based on the tag `MACHINE_NAME` or the
AWS-computed privateDNSName and the domain name specified in the DHCP option set.
The privateDNSName is simply based on the IP address and gives you functional
matching forward/reverse DNS but not a useful name to address your machine.
The `MACHINE_NAME` tag lets you conveniently address the instances you wish to
access via a friendly name. The `Name` tag was not used because not everyone
uses that tag for that purpose.

## Requirements

You need the following:

* VPC with DNS resolution and hostnames enabled
* DHCP Option Set that includes a domain name, corresponding to a *private* zone
in Route 53.
* S3 bucket to hold your CFN templates and Lambda code (does not need to be a dedicated bucket)
* Latest AWS CLI installed to create various things

That's about it.

## Installation
The project can be driven through `make`.

Customize the Makefile S3 parameters to fit your environment.

To create:

```make upload-template upload-lambda create-stack```

To update the function code:

```make upload-lambda update-lambda```

## Notes
This project was forked from the [AWS Labs GitHub repo](https://github.com/awslabs/aws-lambda-ddns-function),
which presented a slightly different approach. This implementation allows the hostname
to be specified via a tag and eliminates the confusing CNAME nomenclature, which can
confuse central authentication mechanisms like Kerberos that expect the client and the
server to agree on the FQDN.
