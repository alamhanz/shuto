from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Size,
    Stack,
)
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_notifications as s3n
from aws_cdk import aws_secretsmanager as sm
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from .bundling import lambda_code

RUNTIME = _lambda.Runtime.PYTHON_3_13
ARCH = _lambda.Architecture.ARM_64


class ShutoStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        ctx = self.node.try_get_context
        model_id: str = ctx("bedrock_model_id")
        upload_enabled: bool = str(ctx("upload_enabled")).lower() == "true"
        notify_email: str | None = ctx("notify_email") or None
        export_cron: str = ctx("export_schedule_cron") or "cron(0 0 ? * MON *)"

        # --- Storage -------------------------------------------------------
        bucket = s3.Bucket(
            self,
            "PhotoBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=False,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    prefix="incoming/",
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INTELLIGENT_TIERING,
                            transition_after=Duration.days(30),
                        )
                    ],
                )
            ],
        )

        table = ddb.Table(
            self,
            "PhotoTable",
            partition_key=ddb.Attribute(name="batch", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="filename", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Shutterstock FTPS credentials. Fill in the real values after first deploy:
        #   {"host": "ftps.shutterstock.com", "username": "...", "password": "..."}
        ftps_secret = sm.Secret(
            self,
            "ShutterstockFtpsSecret",
            description="Shutterstock contributor FTPS credentials for Shuto",
            generate_secret_string=sm.SecretStringGenerator(
                secret_string_template='{"host": "ftps.shutterstock.com", "username": "CHANGE_ME"}',
                generate_string_key="password",
            ),
        )

        topic = sns.Topic(self, "NotifyTopic", display_name="Shuto")
        if notify_email:
            topic.add_subscription(subs.EmailSubscription(notify_email))

        # --- Queue between S3 and Lambda (retries + DLQ, throttles Bedrock) --
        dlq = sqs.Queue(self, "PhotoDLQ", retention_period=Duration.days(14))
        process_timeout = Duration.minutes(5)
        queue = sqs.Queue(
            self,
            "PhotoQueue",
            visibility_timeout=Duration.seconds(process_timeout.to_seconds() * 6),
            dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=3),
            enforce_ssl=True,
        )
        for suffix in (".jpg", ".jpeg", ".JPG", ".JPEG"):
            bucket.add_event_notification(
                s3.EventType.OBJECT_CREATED,
                s3n.SqsDestination(queue),
                s3.NotificationKeyFilter(prefix="incoming/", suffix=suffix),
            )

        # --- Lambdas -------------------------------------------------------
        code = lambda_code(RUNTIME, ARCH)
        common_env = {
            "TABLE_NAME": table.table_name,
            "BUCKET_NAME": bucket.bucket_name,
            "TOPIC_ARN": topic.topic_arn,
        }

        process_fn = _lambda.Function(
            self,
            "ProcessPhotoFn",
            runtime=RUNTIME,
            architecture=ARCH,
            handler="shuto.handlers.process_handler",
            code=code,
            timeout=process_timeout,
            memory_size=1024,
            ephemeral_storage_size=Size.gibibytes(1),
            log_group=logs.LogGroup(
                self,
                "ProcessPhotoLogs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment={
                **common_env,
                "BEDROCK_MODEL_ID": model_id,
                "UPLOAD_ENABLED": "true" if upload_enabled else "false",
                "FTPS_SECRET_ARN": ftps_secret.secret_arn,
            },
        )
        process_fn.add_event_source(
            sources.SqsEventSource(
                queue,
                batch_size=1,
                max_concurrency=3,  # keep Bedrock + FTPS load gentle
                report_batch_item_failures=True,
            )
        )
        bucket.grant_read_write(process_fn)
        table.grant_read_write_data(process_fn)
        ftps_secret.grant_read(process_fn)
        process_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
                ],
            )
        )

        export_fn = _lambda.Function(
            self,
            "ExportCsvFn",
            runtime=RUNTIME,
            architecture=ARCH,
            handler="shuto.handlers.export_handler",
            code=code,
            timeout=Duration.minutes(2),
            memory_size=256,
            log_group=logs.LogGroup(
                self,
                "ExportCsvLogs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment=common_env,
        )
        bucket.grant_read_write(export_fn)
        table.grant_read_write_data(export_fn)
        topic.grant_publish(export_fn)

        events.Rule(
            self,
            "WeeklyExport",
            schedule=events.Schedule.expression(export_cron),
            targets=[targets.LambdaFunction(export_fn)],
        )

        # --- Outputs -------------------------------------------------------
        CfnOutput(self, "BucketName", value=bucket.bucket_name)
        CfnOutput(self, "TableName", value=table.table_name)
        CfnOutput(self, "FtpsSecretName", value=ftps_secret.secret_name)
        CfnOutput(self, "ExportFunctionName", value=export_fn.function_name)
        CfnOutput(self, "DeadLetterQueueUrl", value=dlq.queue_url)
