ROOT_DIR := $(shell dirname $(realpath $(lastword $(MAKEFILE_LIST))))
PARENTDIR := $(realpath ../)
AWS_REGION := us-east-1
S3_BUCKET_NAME  := rhythmic-pub-resources

S3_LAMBDA_BUCKET_PATH := lambda/ddns
S3_LAMBDA_BUCKET_URI := s3://$(S3_BUCKET_NAME)/$(S3_LAMBDA_BUCKET_PATH)

S3_CFN_BUCKET_PATH := cf/ddns
S3_CFN_BUCKET_URI	:= s3://$(S3_BUCKET_NAME)/$(S3_CFN_BUCKET_PATH)

all:
	@echo 'Available make targets:'
	@grep '^[^#[:space:]].*:' Makefile


upload-template:
	@export AWS_REGION=$(AWS_REGION)
	aws s3 cp ddns.template $(S3_CFN_BUCKET_URI)/ddns.template --acl public-read

upload-lambda:
	@export AWS_REGION=$(AWS_REGION)
	zip union.py.zip union.py
	aws s3 cp union.py.zip $(S3_LAMBDA_BUCKET_URI)/union.py.zip --acl public-read
	rm union.py.zip

create-stack:
	@export AWS_REGION=$(AWS_REGION)
	aws cloudformation create-stack --stack-name ddns \
	  --capabilities CAPABILITY_NAMED_IAM \
		--parameters ParameterKey=LambdaBucketName,ParameterValue=$(S3_BUCKET_NAME)  ParameterKey=LambdaBucketKey,ParameterValue=$(S3_LAMBDA_BUCKET_PATH)/union.py.zip \
	  --template-url https://s3.amazonaws.com/$(S3_BUCKET_NAME)/$(S3_CFN_BUCKET_PATH)/ddns.template

# technically this is modifying a stack resource but updating lambda functions
# via cfn is (currently) too much effort.
update-lambda:
	@export AWS_REGION=$(AWS_REGION)
	aws lambda update-function-code --function-name ddns_lambda \
	  --s3-bucket $(S3_BUCKET_NAME) --s3-key $(S3_LAMBDA_BUCKET_PATH)/union.py.zip
