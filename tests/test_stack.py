from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from infra.shuto_stack import ShutoStack


def _template() -> Template:
    app = cdk.App(context={"bedrock_model_id": "jp.anthropic.claude-sonnet-4-6"})
    stack = ShutoStack(
        app, "TestStack", env=cdk.Environment(account="111111111111", region="ap-northeast-1")
    )
    return Template.from_stack(stack)


def test_core_resources():
    t = _template()
    t.resource_count_is("AWS::S3::Bucket", 1)
    t.resource_count_is("AWS::DynamoDB::Table", 1)
    t.resource_count_is("AWS::SQS::Queue", 2)
    t.resource_count_is("AWS::Events::Rule", 1)
    t.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "shuto.handlers.process_handler",
            "Runtime": "python3.13",
            "Architectures": ["arm64"],
            "Environment": {
                "Variables": Match.object_like(
                    {
                        "BEDROCK_MODEL_ID": "jp.anthropic.claude-sonnet-4-6",
                        "UPLOAD_ENABLED": "false",
                    }
                )
            },
        },
    )


def test_bucket_blocks_public_access():
    _template().has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        },
    )


def test_alarm_and_budget_when_email_set():
    app = cdk.App(
        context={
            "bedrock_model_id": "jp.anthropic.claude-sonnet-4-6",
            "notify_email": "me@example.com",
            "monthly_budget_usd": "20",
        }
    )
    stack = ShutoStack(
        app, "T2", env=cdk.Environment(account="111111111111", region="ap-northeast-1")
    )
    t = Template.from_stack(stack)
    t.resource_count_is("AWS::CloudWatch::Alarm", 1)
    t.resource_count_is("AWS::Budgets::Budget", 1)
    t.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "shuto.handlers.export_handler",
            "Environment": {"Variables": Match.object_like({"UPLOAD_ENABLED": "false"})},
        },
    )


def test_lambda_bundle_matches_arm64():
    """Regression: the bundle must contain aarch64 wheels (Lambda runs on arm64)."""
    app = cdk.App(context={"bedrock_model_id": "jp.anthropic.claude-sonnet-4-6"})
    ShutoStack(app, "T3", env=cdk.Environment(account="111111111111", region="ap-northeast-1"))
    out = Path(app.synth().directory)
    wheels = list(out.glob("asset.*/pillow-*.dist-info/WHEEL"))
    assert wheels, "Pillow not bundled"
    tags = wheels[0].read_text()
    assert "aarch64" in tags and "x86_64" not in tags
