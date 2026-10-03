# Runtime dependency notices

The release ZIP contains first-party instructions/Python and reviewed owner-supplied helpers. Optional third-party libraries, FFmpeg, model weights and vendor SDKs are not bundled. Retain each installed distribution's own license files. This is a direct-dependency inventory, not a legal clearance of every future dependency version.

| Optional component | Upstream license/source | Use |
|---|---|---|
| google-api-python-client | [Apache 2.0](https://github.com/googleapis/google-api-python-client/blob/main/LICENSE) | Drive API adapter |
| google-auth-oauthlib | [Apache 2.0](https://github.com/googleapis/google-auth-library-python-oauthlib/blob/main/LICENSE) | Customer OAuth |
| faster-whisper | [MIT](https://github.com/SYSTRAN/faster-whisper/blob/master/LICENSE) | Optional local transcription |
| FFmpeg / ffprobe | [Build-specific LGPL/GPL information](https://ffmpeg.org/legal.html) | Separate system installation, video validation/editing |

Installed transitive packages and model weights carry their own terms. Before redistributing a preinstalled runtime, freeze the actual dependency versions, retain their notices/licenses and inspect the selected FFmpeg build and transcription model. The current package does not distribute such a runtime. Google/Zernio accounts, quotas and OAuth approval remain separate from this software.

The first-party purchaser license is a separate owner decision; this dependency inventory does not grant resale or redistribution rights to the skill.
