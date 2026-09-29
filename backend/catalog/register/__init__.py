"""PR-D1: the whole Government register, captured per exact tozar.

`config`     capacity, group cap and archive configuration (env, defaults)
`archive`    the immutable Cloud Storage object of a captured snapshot
`capture`    the capture job's side: one group of units, each into its own
             snapshot, count-verified and archived before activation
`service`    the API's side: the Register page, Capture, directory refresh
`retention`  what may be pruned (O22), and the list digest
`prune`      the digest-bound prune entrypoint (database rows only)
"""
