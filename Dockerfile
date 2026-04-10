# Lambda container image for the cost reporter.
# Built and pushed by scripts/deploy.sh; deployed by tofu/lambda.tf.
FROM public.ecr.aws/lambda/python:3.12

# Install runtime deps directly with pip — pyproject.toml is the source of
# truth for versions; keep these in sync if you bump them there.
RUN pip install --no-cache-dir \
      'boto3>=1.34' \
      'polars>=1.0' \
      'matplotlib>=3.8' \
      'seaborn>=0.13'

COPY cost_reporter.py lambda_handler.py ${LAMBDA_TASK_ROOT}/

CMD ["lambda_handler.handler"]
