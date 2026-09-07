# lwIP netif-list/default reproduction

This is a public, deterministic reproduction, not a production incident.

- Official upstream: `https://git.savannah.nongnu.org/git/lwip.git`
- Test mirror: `https://github.com/lwip-tcpip/lwip`
- Revision: `3d896ba0a37ff3ce73270ca5e230707fe47f60e3`
- Command: `./examples/reproductions/lwip-netif/run.sh`
- Expected output: [`result.txt`](result.txt)
- Environment used for the checked result: macOS 27.0, arm64, Apple clang 21.0.0

The harness uses a minimal public `lwipopts.h`. It adds two interfaces, explicitly
sets the first as default, and records `netif_list`, `netif_default`, and return
values. The result demonstrates that list insertion and default-interface
selection are separate operations under this pinned configuration.

If the workspace checkout is cloud-evicted, set `CODEATLAS_LWIP_SOURCE_ROOT` to
another checkout at the pinned revision.

## Artifact hashes (SHA-256)

- `reproduce.c`: `cafe3b447e59500c9dbb677a92866bda14b6a78f35594362dfba6ffe5af9e32a`
- `run.sh`: `82f26ccf13e73a3e8e19b12629a466c3b74045a3659203a0d414044f1fd34528`
- `include/lwipopts.h`: `65426d6f6d1c1a97a74392bfe626d19e68faf00ee585f5990231edd5edf2ecf2`
- `result.txt`: `8102733501276cd10303a81dc15877f7e5386b1dacb48c535f2960562f95414f`
