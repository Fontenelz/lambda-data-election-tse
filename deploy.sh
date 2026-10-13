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

# não publica código com teste quebrado (SEM_TESTES=1 pula, só em emergência)
if [ "${SEM_TESTES:-0}" != "1" ]; then
  echo "==> Testes"
  if command -v python3 >/dev/null; then PY=(python3); else PY=(uv run --no-project --python 3.14 python); fi
  (cd "$DIR" && "${PY[@]}" -m unittest discover -s tests -q)
fi

echo "==> Infraestrutura (stack $STACK em $REGION)"
aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-file "$DIR/template.yaml" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  "${OVERRIDES[@]}"

# mesmo zip para o espelho e as duas funções do feed (handlers diferentes);
# local.py é só o modo CLI
(cd "$DIR/coletor" && zip -q -r "$BUILD/coletor.zip" . -x '*__pycache__*' -x 'local.py')
for SAIDA in FunctionName FeedFunctionName FeedMunicipiosFunctionName; do
  FUNCTION=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$SAIDA'].OutputValue" --output text)
  echo "==> Código da Lambda $FUNCTION"
  aws lambda update-function-code --region "$REGION" --function-name "$FUNCTION" \
    --zip-file "fileb://$BUILD/coletor.zip" --query 'LastUpdateStatus' --output text
  aws lambda wait function-updated-v2 --region "$REGION" --function-name "$FUNCTION"
done

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

# alarmes do CloudFront: as métricas dele só existem em us-east-1. Parâmetros
# vêm do stack principal (distribuição, notificador e e-mail de alerta).
echo "==> Alarmes do CloudFront (stack $STACK-borda em us-east-1)"
saida() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
DISTRIBUTION=$(saida DistributionId)
NOTIFIER_ARN=$(saida NotifierFunctionArn)
[ "$NOTIFIER_ARN" = "None" ] && NOTIFIER_ARN=""
ALERT_EMAIL=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Parameters[?ParameterKey=='AlertEmail'].ParameterValue" --output text)
[ "$ALERT_EMAIL" = "None" ] && ALERT_EMAIL=""
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name "$STACK-borda" \
  --template-file "$DIR/template-borda.yaml" \
  --no-fail-on-empty-changeset \
  --parameter-overrides "DistributionId=$DISTRIBUTION" "NotifierFunctionArn=$NOTIFIER_ARN" "AlertEmail=$ALERT_EMAIL"

aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query 'Stacks[0].Outputs' --output table
