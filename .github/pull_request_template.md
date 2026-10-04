## What and why

## Checklist

- [ ] Tests pass locally (`python3 agent/test_agent.py`; the status page tests; `helm lint`), and new logic has a test
- [ ] The README / CONTRIBUTING are updated if behavior or values changed
- [ ] If the metric, its dimensions, or detector names changed: agent, status page, chart and `splunk/` are all updated (see "The contract between the parts")
- [ ] New or changed profiles: tested against a live install (paste the leader's logs), and the README "Tested on" column is updated
