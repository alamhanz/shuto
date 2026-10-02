import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from infra.github_oidc_stack import GithubOidcStack

ENV = cdk.Environment(account="111111111111", region="ap-northeast-1")


def _template(context=None) -> Template:
    app = cdk.App(context=context or {})
    return Template.from_stack(GithubOidcStack(app, "Oidc", env=ENV))


def test_role_trusts_only_production_environment_of_repo():
    t = _template()
    t.resource_count_is("AWS::IAM::OIDCProvider", 1)
    t.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": {
                "Statement": [
                    Match.object_like(
                        {
                            "Action": "sts:AssumeRoleWithWebIdentity",
                            "Condition": {
                                "StringEquals": {
                                    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                                    "token.actions.githubusercontent.com:sub": (
                                        "repo:alamhanz/shuto:environment:production"
                                    ),
                                }
                            },
                        }
                    )
                ]
            }
        },
    )


def test_role_can_only_assume_cdk_bootstrap_roles():
    _template().has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": [
                    {
                        "Action": ["sts:AssumeRole", "sts:TagSession"],
                        "Effect": "Allow",
                        "Resource": "arn:aws:iam::111111111111:role/cdk-hnb659fds-*-111111111111-*",
                    }
                ]
            }
        },
    )


def test_reuses_existing_provider():
    arn = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
    t = _template({"github_oidc_provider_arn": arn})
    t.resource_count_is("AWS::IAM::OIDCProvider", 0)
