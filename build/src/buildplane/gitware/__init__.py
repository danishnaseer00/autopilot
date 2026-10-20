"""Git integration (SPEC-08).

Public surface:

* `WebhookService` + `create_github_webhook_router` — the receiver;
* `derive_webhook_secret`, `sign_payload`, `verify_signature` — R-3..R-6;
* `OwnershipVerifier` — R-8/R-9;
* `GitStore` / `InMemoryGitStore` — the persistence port and the test adapter;
* `OAuthStateSigner` — the signed, single-use OAuth state primitive.

The Postgres adapter and the OAuth endpoints are wired by the control plane;
this package owns the webhook and verification domain.
"""

from buildplane.gitware.api import (
    PayloadTooLarge,
    create_github_webhook_router,
    read_bounded_body,
)
from buildplane.gitware.errors import (
    DeploymentBlocked,
    GitwareError,
    InvalidGitRef,
    InvalidGitSha,
    InvalidPreviewDomain,
    InvalidProviderRepoId,
    InvalidVerificationToken,
    OAuthStateError,
    VerificationUnavailable,
    WebhookHeaderError,
    WebhookPayloadError,
)
from buildplane.gitware.events import (
    PingEvent,
    PullRequestEvent,
    PushEvent,
    RepositoryEvent,
    UnsupportedEvent,
    decode_payload,
    parse_event,
)
from buildplane.gitware.models import (
    BranchRule,
    DeploymentIntent,
    DeploymentRecord,
    DeploymentStatus,
    Environment,
    Project,
    ProjectStatus,
    RecordResult,
    RouteMatch,
    RouteSource,
    TriggerSource,
    VerificationMethod,
    WebhookEventRecord,
    WebhookOutcome,
)
from buildplane.gitware.oauth import (
    InMemoryNonceStore,
    NonceStore,
    OAuthState,
    OAuthStateSigner,
)
from buildplane.gitware.previews import (
    PreviewHostnameAllocator,
    branch_hostname_candidate,
    pull_request_hostname_candidate,
)
from buildplane.gitware.ratelimit import (
    DEFAULT_BURST_LIMIT,
    InMemoryRateLimiter,
    WebhookRateLimiter,
)
from buildplane.gitware.routing import (
    CATCH_ALL_REF,
    build_pull_request_intent,
    build_push_intent,
    pull_request_ref,
    route_push,
)
from buildplane.gitware.service import MAX_WEBHOOK_BYTES, WebhookService
from buildplane.gitware.signature import (
    SIGNATURE_HEADER,
    derive_unresolved_secret,
    derive_webhook_secret,
    sign_payload,
    verify_signature,
)
from buildplane.gitware.store import GitStore, InMemoryGitStore
from buildplane.gitware.validation import (
    is_zero_sha,
    validate_git_ref,
    validate_git_sha,
    validate_pr_number,
    validate_preview_domain,
    validate_provider_repo_id,
)
from buildplane.gitware.verify import (
    DEFAULT_REVERIFY_INTERVAL,
    MAX_VERIFY_BRANCHES,
    TXT_SUFFIX,
    VERIFY_BRANCH_PREFIX,
    VERIFY_FILE,
    OwnershipVerifier,
    RepoReader,
    TxtResolver,
    VerificationResult,
    new_verification_token,
)

__all__ = [
    "CATCH_ALL_REF",
    "DEFAULT_BURST_LIMIT",
    "DEFAULT_REVERIFY_INTERVAL",
    "MAX_VERIFY_BRANCHES",
    "MAX_WEBHOOK_BYTES",
    "SIGNATURE_HEADER",
    "TXT_SUFFIX",
    "VERIFY_BRANCH_PREFIX",
    "VERIFY_FILE",
    "BranchRule",
    "DeploymentBlocked",
    "DeploymentIntent",
    "DeploymentRecord",
    "DeploymentStatus",
    "Environment",
    "GitStore",
    "GitwareError",
    "InMemoryGitStore",
    "InMemoryNonceStore",
    "InMemoryRateLimiter",
    "InvalidGitRef",
    "InvalidGitSha",
    "InvalidPreviewDomain",
    "InvalidProviderRepoId",
    "InvalidVerificationToken",
    "NonceStore",
    "OAuthState",
    "OAuthStateError",
    "OAuthStateSigner",
    "OwnershipVerifier",
    "PayloadTooLarge",
    "PingEvent",
    "PreviewHostnameAllocator",
    "Project",
    "ProjectStatus",
    "PullRequestEvent",
    "PushEvent",
    "RecordResult",
    "RepoReader",
    "RepositoryEvent",
    "RouteMatch",
    "RouteSource",
    "TriggerSource",
    "TxtResolver",
    "UnsupportedEvent",
    "VerificationMethod",
    "VerificationResult",
    "VerificationUnavailable",
    "WebhookEventRecord",
    "WebhookHeaderError",
    "WebhookOutcome",
    "WebhookPayloadError",
    "WebhookRateLimiter",
    "WebhookService",
    "branch_hostname_candidate",
    "build_pull_request_intent",
    "build_push_intent",
    "create_github_webhook_router",
    "decode_payload",
    "derive_unresolved_secret",
    "derive_webhook_secret",
    "is_zero_sha",
    "new_verification_token",
    "parse_event",
    "pull_request_hostname_candidate",
    "pull_request_ref",
    "read_bounded_body",
    "route_push",
    "sign_payload",
    "validate_git_ref",
    "validate_git_sha",
    "validate_pr_number",
    "validate_preview_domain",
    "validate_provider_repo_id",
    "verify_signature",
]
