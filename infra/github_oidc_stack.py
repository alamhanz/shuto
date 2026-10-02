"""One-time stack: lets GitHub Actions deploy ShutoStack without stored AWS keys.

GitHub's OIDC token is exchanged for a short-lived role. The role can only assume the
CDK bootstrap roles (deploy / publish assets / lookup), the pattern CDK recommends for CI.
Only workflows running in this repo's `production` environment can assume it.

Deploy it yourself once:  cdk deploy ShutoGithubOidc
(If your account already has the GitHub OIDC provider, pass
 -c github_oidc_provider_arn=arn:aws:iam::<acct>:oidc-provider/token.actions.githubusercontent.com)
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"
DEPLOY_ENVIRONMENT = "production"


class GithubOidcStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = self.node.try_get_context
        repo: str = ctx("github_repo") or "alamhanz/shuto"
        existing_arn: str | None = ctx("github_oidc_provider_arn") or None
        qualifier: str = ctx("@aws-cdk/core:bootstrapQualifier") or "hnb659fds"

        if existing_arn:
            provider = iam.OidcProviderNative.from_oidc_provider_arn(
                self, "GithubOidc", existing_arn
            )
        else:
            provider = iam.OidcProviderNative(
                self,
                "GithubOidc",
                url=f"https://{GITHUB_OIDC_HOST}",
                client_ids=["sts.amazonaws.com"],
            )

        role = iam.Role(
            self,
            "GithubDeployRole",
            role_name="shuto-github-deploy",
            description=f"GitHub Actions deploy role for {repo} ({DEPLOY_ENVIRONMENT})",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(
                provider.oidc_provider_arn,
                conditions={
                    "StringEquals": {
                        f"{GITHUB_OIDC_HOST}:aud": "sts.amazonaws.com",
                        f"{GITHUB_OIDC_HOST}:sub": (
                            f"repo:{repo}:environment:{DEPLOY_ENVIRONMENT}"
                        ),
                    }
                },
            ),
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole", "sts:TagSession"],
                resources=[f"arn:aws:iam::{self.account}:role/cdk-{qualifier}-*-{self.account}-*"],
            )
        )

        CfnOutput(self, "DeployRoleArn", value=role.role_arn)
