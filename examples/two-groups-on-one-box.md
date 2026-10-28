# Two groups on one box (your group vs your mom's group)

One relay process can serve two unrelated families without letting either see the
other's clipboard. A **group** is the triple `(group_id, token, e2e_key)`:

| | your group | your mom's group |
|---|---|---|
| `group_id` | `family-a` | `family-b` |
| `token` | `CHANGE-ME-token-family-a` | `CHANGE-ME-token-family-b` |
| `e2e_key` | only on your devices | only on her devices |
| relay entry | `[[serve_groups]] id = "family-a"` | `[[serve_groups]] id = "family-b"` |

What the relay knows: both group ids, both group tokens (needed to authenticate
clients), device ids and roster membership, frame sizes/timing and ciphertext.
What the relay never knows: either `e2e_key`, so it cannot read or forge clipboard
content. Each group's members refuse frames from the other group because the
group id is part of the authenticated room and the e2e key differs.

## On one laptop (today)

1. `examples/relay.toml` already has both `serve_groups` entries. Replace both
   `CHANGE-ME-token-...` values with the real tokens; the relay needs **no** keys.
2. Give each group its own member file (`examples/member-laptop.toml` is
   `family-a`): set `group_id` to the right group, copy that group's token, put its
   key in `e2e_key` (or `e2e_key_file`), and list only its own devices under
   `[[peers]]`.
3. Run one relay with the relay config and one daemon per member config. Both
   families can point at the same relay address; they stay separate.

The relay may run on this laptop (`bind = "127.0.0.1"`) for the two-process smoke
test, or later on a small always-on box. When it runs on another machine, keep the
bind address on a private tailnet/trusted LAN and read the token/TLS caveat in the
README: tokens still cross `ws://` in cleartext until TLS is added.

## Not in scope here

A device that belongs to both groups, cross-group sharing, and the Android client
are out of scope. The phone example is a documented placeholder only, not a working
client.
