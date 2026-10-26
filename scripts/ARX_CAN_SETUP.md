# ARX CAN initialization on enable

The explicit arm enable action initializes can1, can3 and can6 using the same
`slcand -o -f -s8 /dev/arxcanN canN` and `ip link set canN up` operations as
`01_collect.sh`. It never starts that interactive launcher or the legacy recorder.
The USB adapters must have the existing root-managed `/dev/arxcanN` udev mappings.

Install `scripts/init_arx_can.py` as
`/usr/local/libexec/bodypipeline-can-init`, owned by root:root, mode 0755. This
installed copy must not be writable by the service account. Configure sudoers
with only these three commands (replace `bodypipeline` if deploying under another account):

```sudoers
bodypipeline ALL=(root) NOPASSWD: /usr/local/libexec/bodypipeline-can-init can1, /usr/local/libexec/bodypipeline-can-init can3, /usr/local/libexec/bodypipeline-can-init can6
```

Validate the sudoers file with `visudo -cf` before installing it, mode 0440.
The helper uses a fixed interface allowlist, absolute command paths, isolated
Python, and a lock per interface. It does not kill other adapters' slcand.

No CAN operation happens on Bridge startup. If CAN is lost, the next explicit
enable stops only the Bridge's own controller/joy processes before restoring the
interfaces, then launches fresh controllers and waits for new observations.
An externally managed controller must be closed by its owner before recovery.
