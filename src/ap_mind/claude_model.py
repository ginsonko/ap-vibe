"""Claude's local execution identity is independent of the gateway's model.

The gateway always binds the configured upstream model. A stable CLI alias
avoids client-side validation of arbitrary provider model names. This module
does not change saved model names or route a request to a different provider.
"""
from .contracts import ContractError


def normalize_cli_model(value):
    if not isinstance(value, str) or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ContractError('agent_cli_model_invalid')
    return value.strip()


def claude_cli_model(profile):
    return normalize_cli_model(profile.get('cli_model', '')) or 'sonnet'


def claude_model_environment(profile):
    model = claude_cli_model(profile)
    return {key: model for key in (
        'ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL',
        'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL',
    )}
