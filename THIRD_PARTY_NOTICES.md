# Third-party components

Lemonbot's own source and documentation are licensed under [MIT](LICENSE).
This grant does not apply to third-party software, services, images or WeChat.
Python dependency versions are in [uv.lock](uv.lock); each dependency retains its
own license. Installation downloads dependencies, not a bundled distribution.

## agent-wechat / WeChat

Deployment references (does not redistribute) this AMD64 image:

```
ghcr.io/thisnick/agent-wechat@sha256:97d5a99f933592c15897a7579b4e2b2cbbb5f4dda856228f8271f91dd7cafb43
```

The researched upstream commit `96c2e90c497d999bf5189a134a9b3b293731f42e`
had no license file or package license declaration. Author permission was not
established. The image has no verified source revision association. See
[the recorded license inspection](docs/evidence/agent-wechat-license-check-20261008.json).
Lemonbot communicates with its local HTTP API using independently implemented code.
It does not copy upstream source or ship the image, WeChat installer or account data.
This notice grants no rights to those components; assess the upstream terms before
reuse or redistribution. The frozen PR #171 candidate is not part of 1.0 deployment.

Experimental Orin deployment separately references the ARM64 0.7.4 image pinned in
[its guide](docs/deployment-jetson-orin.md). Complete image/source association remains
unverified; a matching helper file does not verify the whole build.

The Orin browser bridge downloads Ubuntu's noVNC 1.3.0-2 static resources into the
desktop user's data directory. [noVNC](https://novnc.com/info.html) is MPL-2.0; its
copyright/license files are retained locally. No noVNC assets are bundled in this
MIT source repository. WebSocket dependency versions and hashes are in uv.lock.

DeepSeek is an external paid service subject to its own terms and pricing.
