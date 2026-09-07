#!/bin/bash
# Runs inside localstack once it's ready (mounted to /etc/localstack/init/ready.d).
# Creates the telemetry queue and a dead-letter queue, wired with a redrive policy
# so a message that fails to store 5 times parks in the DLQ instead of looping.
set -euo pipefail

awslocal sqs create-queue --queue-name dialpass-telemetry-dlq

DLQ_ARN=$(awslocal sqs get-queue-attributes \
  --queue-url http://localhost:4566/000000000000/dialpass-telemetry-dlq \
  --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)

awslocal sqs create-queue --queue-name dialpass-telemetry \
  --attributes RedrivePolicy="{\"deadLetterTargetArn\":\"${DLQ_ARN}\",\"maxReceiveCount\":\"5\"}"

echo "localstack-init: dialpass-telemetry queue ready"
