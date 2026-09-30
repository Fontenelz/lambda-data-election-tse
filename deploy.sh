#!/usr/bin/env bash
# Publica o coletor TSE na AWS: infraestrutura via CloudFormation + código da Lambda.
# Uso: aws/deploy.sh [Parametro=Valor ...]
#   ex.: aws/deploy.sh ScheduleExpression="rate(1 minute)" TseAmbiente=oficial
set -euo pipefail

REGION="${AWS_REGION:-sa-east-1}"
STACK="${STACK_NAME:-coletor-tse}"
DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

OVERRIDES=()
[ $# -gt 0 ] && OVERRIDES=(--parameter-overrides "$@")

echo "==> Infraestrutura (stack $STACK em $REGION)"
aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-file "$DIR/template.yaml" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  "${OVERRIDES[@]}"

echo "==> Código da Lambda"
FUNCTION=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='FunctionName'].OutputValue" --output text)
(cd "$DIR/coletor" && zip -q -r "$BUILD/coletor.zip" . -x '__pycache__/*')
aws lambda update-function-code --region "$REGION" --function-name "$FUNCTION" \
  --zip-file "fileb://$BUILD/coletor.zip" --query 'LastUpdateStatus' --output text
aws lambda wait function-updated-v2 --region "$REGION" --function-name "$FUNCTION"

# notificador só existe quando TelegramChatId está configurado
NOTIFIER=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='NotifierFunctionName'].OutputValue" --output text)
if [ -n "$NOTIFIER" ] && [ "$NOTIFIER" != "None" ]; then
  echo "==> Código do notificador Telegram"
  (cd "$DIR/notificador" && zip -q -r "$BUILD/notificador.zip" . -x '__pycache__/*')
  aws lambda update-function-code --region "$REGION" --function-name "$NOTIFIER" \
    --zip-file "fileb://$BUILD/notificador.zip" --query 'LastUpdateStatus' --output text
  aws lambda wait function-updated-v2 --region "$REGION" --function-name "$NOTIFIER"
fi

aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query 'Stacks[0].Outputs' --output table
