# Tenant process sandbox profile

`seccomp-runtime.json` derives from the Moby default allowlist at commit
`f862455628f78f84c937e967e4a03902fb1dee11` (Apache 2.0, included license).
Only clone, unshare, setns, mount, umount2, pivot_root and sethostname are added
for bubblewrap's nested unprivileged user/PID/mount namespaces. All remaining
default syscall exclusions remain, including clone3's ENOSYS fallback.

The tenant runs as uid 10001 under a dedicated rootless daemon, with all outer
capabilities removed, no-new-privileges, a read-only root, a single private
volume, and CPU/memory/PID limits. Namespace capabilities cannot cross into
the parent user namespace. The Docker socket and cloud control-plane data are
never mounted. `systempaths=unconfined` permits the child PID namespace to
mount its own `/proc`; Docker's masked parent proc otherwise prevents this.

The original engine then starts each Bash/Python process in a second
bubblewrap sandbox: empty root, read-only interpreter/system resources, only
the execution workspace writable, sanitized environment, private PID tree,
and isolated network unless that tool's explicit policy allows network.
Failure to create this sandbox remains a hard execution failure.

The profile is consumed by the trusted Docker client, so it must be shipped
with the API image as well as source installs. Render jobs keep Docker's
unmodified default profile and never use this nested-execution exception.

Sources: [Docker seccomp documentation](https://docs.docker.com/engine/security/seccomp/)
and [pinned Moby profile](https://github.com/moby/profiles/blob/f862455628f78f84c937e967e4a03902fb1dee11/seccomp/default.json).
