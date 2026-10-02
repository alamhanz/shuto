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
