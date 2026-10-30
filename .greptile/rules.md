# Greptile rules — Vycari/pepper-cli

Review rules specific to this repo. The `/api/v1` contract rule is the one that matters most:

- The CLI mirrors the Pepper server's `/api/v1`. A new dataclass or field in `api_client.py` that
  mirrors a server model must carry a `# MIRROR: src/pepper/api/<module>.py::<Model>` comment.
- The CLI must tolerate unknown response fields and unknown SSE frame kinds. Flag any change that
  makes an unrecognized field or frame an error.
- A token must never be logged, printed, or put in an exception message. Tokens are held as
  `SecretStr` and unwrapped only at the point of use.

## Greptile review controls

Vycari Review reviews pull requests first and posts `@greptileai review` once, when it is
happy; Greptile is the final gate. Agents never request a Greptile review themselves.

To skip review for one pull request (both Vycari Review and Greptile), apply the
`greptile:skip` label. To temporarily avoid review while work is incomplete, keep the pull
request in draft. To request review later, remove the label; a human may comment
`@greptileai` to ask for a Greptile review directly.
