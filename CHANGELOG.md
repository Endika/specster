# Changelog

## [0.8.1](https://github.com/Endika/specster/compare/specster-v0.8.0...specster-v0.8.1) (2026-09-28)


### Documentation

* make the README a short install, use and cost guide and move the reference to docs/ ([0b582c6](https://github.com/Endika/specster/commit/0b582c6a9b7ecb8ed45dd542d4408bbedd65e558))

## [0.8.0](https://github.com/Endika/specster/compare/specster-v0.7.0...specster-v0.8.0) (2026-09-28)


### Features

* **cli:** add --help flag to python -m specster ([ff90f11](https://github.com/Endika/specster/commit/ff90f116874ef0653b466605a0d87615d37ddf85))

## [0.7.0](https://github.com/Endika/specster/compare/specster-v0.6.0...specster-v0.7.0) (2026-09-28)


### Features

* stop a build whose tests already fail on the base, before any worker is paid ([931c387](https://github.com/Endika/specster/commit/931c38777347494ae9a135a305743f1b2d54ad76))


### Documentation

* explain the base test run and build.allow_failing_base ([f50d1fe](https://github.com/Endika/specster/commit/f50d1fecd5658b2f93da6d02ff5579ea346e2b4e))

## [0.6.0](https://github.com/Endika/specster/compare/specster-v0.5.9...specster-v0.6.0) (2026-09-28)


### Features

* cap tasks at five files in the planner prompt and flag bigger ones in the spec ([997d95c](https://github.com/Endika/specster/commit/997d95c9c76339b19ade354ba9108df05d29373e))

## [0.5.9](https://github.com/Endika/specster/compare/specster-v0.5.8...specster-v0.5.9) (2026-09-28)


### Bug Fixes

* give killed slot processes 30 s to exit, since a starved runner keeps them running ([df8a7c2](https://github.com/Endika/specster/commit/df8a7c2c6ca9988f337d5b221d65e7c05caf4b68))
* let sandbox slots read the tree-sitter grammars baked into the image ([5b8f4bd](https://github.com/Endika/specster/commit/5b8f4bd04fc7116a4efe5d9fd3ee7bd30fd00343))

## [0.5.8](https://github.com/Endika/specster/compare/specster-v0.5.7...specster-v0.5.8) (2026-09-28)


### Bug Fixes

* refuse a TMPDIR in build.test_env, which would reopen the shared /tmp ([0d6e139](https://github.com/Endika/specster/commit/0d6e1394159832ae059da9b6facf0a1ca7e07da6))


### Documentation

* explain why /tmp and /dev/shm stay shared between slots ([c3b9f93](https://github.com/Endika/specster/commit/c3b9f930c8864d71c8f3c912307d2076b04edb2a))

## [0.5.7](https://github.com/Endika/specster/compare/specster-v0.5.6...specster-v0.5.7) (2026-09-28)


### Bug Fixes

* stop running workers at their next turn once another hits a fatal error ([8f9cc3e](https://github.com/Endika/specster/commit/8f9cc3e207a1dfc60cf35a4b8db0bd80d6d1a166))

## [0.5.6](https://github.com/Endika/specster/compare/specster-v0.5.5...specster-v0.5.6) (2026-09-28)


### Bug Fixes

* stop the reviewer at the build's time limit like the workers ([b901c15](https://github.com/Endika/specster/commit/b901c1515422948cb1d421f2fb958e785db67bb0))


### Documentation

* say the time limit also stops the reviewer ([decb1b5](https://github.com/Endika/specster/commit/decb1b54b569f0e118a6d38effb1459f3336697f))

## [0.5.5](https://github.com/Endika/specster/compare/specster-v0.5.4...specster-v0.5.5) (2026-09-28)


### Bug Fixes

* keep the build's tasks, tests and skills in the footer of a failed run ([a0c993d](https://github.com/Endika/specster/commit/a0c993d54700860e75710989a131a5a4ee88f113))
* tell the planner to write spec fields as plain text, not JSON escapes ([f6aae19](https://github.com/Endika/specster/commit/f6aae1913dd15d93696a822e34ad985383465507))

## [0.5.4](https://github.com/Endika/specster/compare/specster-v0.5.3...specster-v0.5.4) (2026-09-28)


### Bug Fixes

* leave one-line code spans unescaped so GitHub shows what the model wrote ([6a539e3](https://github.com/Endika/specster/commit/6a539e399729617496aba4e3ad7e75019767202f))

## [0.5.3](https://github.com/Endika/specster/compare/specster-v0.5.2...specster-v0.5.3) (2026-09-28)


### Bug Fixes

* neutralise closing references hidden in Markdown links and autolinks ([ab541d6](https://github.com/Endika/specster/commit/ab541d6c24f672e600ca713b6cdfb1f4c3ad456d))

## [0.5.2](https://github.com/Endika/specster/compare/specster-v0.5.1...specster-v0.5.2) (2026-09-28)


### Documentation

* name roadmap milestones after their releases ([72c1dc3](https://github.com/Endika/specster/commit/72c1dc3082e7f31f779d60ec5232cccc3685aa13))

## [0.5.1](https://github.com/Endika/specster/compare/specster-v0.5.0...specster-v0.5.1) (2026-09-28)


### Documentation

* record the measured build costs from the dogfood runs ([6f653ec](https://github.com/Endika/specster/commit/6f653eca315fa41380eee44fb2d030237aec49f8))

## [0.5.0](https://github.com/Endika/specster/compare/specster-v0.4.4...specster-v0.5.0) (2026-09-28)


### Features

* author build commits with the bot's noreply email so GitHub shows its avatar ([4979929](https://github.com/Endika/specster/commit/4979929b2a7e4a541c415eba19ca46412d109f2c))


### Documentation

* explain the build commits' author email ([54d0639](https://github.com/Endika/specster/commit/54d063959d8a22dc4cdac3c32f5e962ff5b31374))

## [0.4.4](https://github.com/Endika/specster/compare/specster-v0.4.3...specster-v0.4.4) (2026-09-28)


### Bug Fixes

* refuse builds that change Specster's own config unless allowed ([df78f65](https://github.com/Endika/specster/commit/df78f6579e97e89ff55370b762fa55b4e4aa471c))


### Documentation

* document build.allow_config_changes ([6b44f01](https://github.com/Endika/specster/commit/6b44f01cb6fcb85251d9859644ab53d96ea27933))

## [0.4.3](https://github.com/Endika/specster/compare/specster-v0.4.2...specster-v0.4.3) (2026-09-28)


### Bug Fixes

* **cli:** reject unknown -- flags with usage and exit 2 ([11c963e](https://github.com/Endika/specster/commit/11c963e006659ecec6001267ebcf5ea17e73ebf2))

## [0.4.2](https://github.com/Endika/specster/compare/specster-v0.4.1...specster-v0.4.2) (2026-09-28)


### Bug Fixes

* run the image under tini so killed orphans are reaped during a build ([087aff1](https://github.com/Endika/specster/commit/087aff1add0f82b0d9e2879653194380629755d9))

## [0.4.1](https://github.com/Endika/specster/compare/specster-v0.4.0...specster-v0.4.1) (2026-09-28)


### Bug Fixes

* show a failed task's last test run in the build comment ([b3e5708](https://github.com/Endika/specster/commit/b3e5708484af49a02c2c6bd57ba897eca59216b9))

## [0.4.0](https://github.com/Endika/specster/compare/specster-v0.3.2...specster-v0.4.0) (2026-09-24)


### Features

* add the ai-build phase that builds the approved plan into a pull request ([00fc023](https://github.com/Endika/specster/commit/00fc023a22fe8d52ba145d99c72c94b7c093c01a))


### Documentation

* document the build phase, its isolation, permissions and refusals ([b1cf486](https://github.com/Endika/specster/commit/b1cf48672291253f838593a688c96190c24090eb))

## [0.3.2](https://github.com/Endika/specster/compare/specster-v0.3.1...specster-v0.3.2) (2026-09-24)


### Documentation

* add a full reference of the action's inputs, output and keyless providers ([0ba84cd](https://github.com/Endika/specster/commit/0ba84cdb23997a7cdafcfe97ca995e25155562f9))

## [0.3.1](https://github.com/Endika/specster/compare/specster-v0.3.0...specster-v0.3.1) (2026-09-24)


### Performance Improvements

* **llm:** cache the whole conversation with a breakpoint on the newest message ([7e30663](https://github.com/Endika/specster/commit/7e306633057e93d12834efc8fedde11b003aa635))

## [0.3.0](https://github.com/Endika/specster/compare/specster-v0.2.0...specster-v0.3.0) (2026-09-24)


### Features

* **skills:** load every skill of the phase by default ([9bc7708](https://github.com/Endika/specster/commit/9bc7708fbbb0468a3421a66106ac07602a907742))

## [0.2.0](https://github.com/Endika/specster/compare/specster-v0.1.2...specster-v0.2.0) (2026-09-24)


### Features

* **persona:** add a concise style and a question cap for shorter comments ([2006a39](https://github.com/Endika/specster/commit/2006a39cacfb955ef310a97e40fd67f14b839b13))

## [0.1.2](https://github.com/Endika/specster/compare/specster-v0.1.1...specster-v0.1.2) (2026-09-24)


### Bug Fixes

* **release:** publish vX.Y.Z image and git tags that action.yml points to ([2332b99](https://github.com/Endika/specster/commit/2332b99f6a3c80055d547b749030346bcdf475d6))

## [0.1.1](https://github.com/Endika/specster/compare/specster-v0.1.0...specster-v0.1.1) (2026-09-24)


### Documentation

* shrink the mascot to sit next to the title ([6b653b8](https://github.com/Endika/specster/commit/6b653b8aede296aad7b986d8e6786c4076074f26))

## 0.1.0 (2026-09-24)


### Features

* **agent:** add provider-neutral chat interface and the submit-terminated tool loop ([74c94a9](https://github.com/Endika/specster/commit/74c94a9f10ab693485f00534389108a21a21d317))
* **config:** add validated config schema and loader ([b0c928e](https://github.com/Endika/specster/commit/b0c928eb9b695c90d7643a9ccbf8783da97ad8f3))
* **event:** parse issue label and dispatch triggers with bot guard ([df5922e](https://github.com/Endika/specster/commit/df5922ef2be7a3fca4e122739b58b8cd83ebda5a))
* **github:** add issue tracker port and REST adapter ([04fae21](https://github.com/Endika/specster/commit/04fae211a8157451a10db3bd451b151abce90cce))
* **llm:** add Anthropic, Bedrock and Vertex Claude adapters with replayed contract test ([ab72553](https://github.com/Endika/specster/commit/ab72553885015a74f0063f5f1da4e6e64fb04dd3))
* **llm:** add Gemini and Vertex Gemini adapters with backoff ([339845f](https://github.com/Endika/specster/commit/339845f95eec2a852de7f633f7ccc88a16664804))
* **llm:** add OpenAI, OpenAI-compatible and Azure OpenAI adapters ([a97a524](https://github.com/Endika/specster/commit/a97a524ad98698ccffab7e5c3c487830c7b3b52b))
* **metrics:** add pricing, run metrics marker and budget sum ([13d573c](https://github.com/Endika/specster/commit/13d573ca624b3ad61632e2a3691e83ff25786733))
* **plan:** validate task graph and serialize tasks that share files ([22ed666](https://github.com/Endika/specster/commit/22ed666129c1317b8c414b082ed9efeed2c3add9))
* **render:** render questions, spec, error and budget comments with metrics ([cabb64d](https://github.com/Endika/specster/commit/cabb64de2429ae307325f4b99e35b04f6284d08d))
* **repomap:** map files and top-level symbols with a reported token cap ([b14ae5c](https://github.com/Endika/specster/commit/b14ae5c25b00b271154488aa0f550155113d2f43))
* **run:** orchestrate the spec phase end to end ([da626b4](https://github.com/Endika/specster/commit/da626b4b00ac68d3314cd6024f159876ca5932e8))
* **sanitize:** strip and report hidden HTML comments and invisible characters ([d5647ef](https://github.com/Endika/specster/commit/d5647efbfb8287aa164dcdb552d4ef0d3df0f999))
* **schemas:** add questions and spec submission schemas ([22b5554](https://github.com/Endika/specster/commit/22b55547391895057ce33746e43ce6fac9815ffc))
* **skills:** discover, pin and phase project skills ([0918d73](https://github.com/Endika/specster/commit/0918d73973e185d77cbe9ac15269fa2f74c08250))
* **thread:** build the trusted issue snapshot the agent reads ([7e431f6](https://github.com/Endika/specster/commit/7e431f6a89ddd991fa6ee9fe413d2438bb50feec))
* **workspace:** add jailed list, read and grep tools ([0d73446](https://github.com/Endika/specster/commit/0d73446262fc79db9da9c3b3534e34e852532e5d))


### Bug Fixes

* **agent:** bill the usage of failed model runs against the issue budget ([1508b99](https://github.com/Endika/specster/commit/1508b993c33204619b3c4c0870ba867623df3a2e))
* **agent:** return argument type errors to the model ([1de3853](https://github.com/Endika/specster/commit/1de38537321a9b581a2ea77ad64cfbdc3060e01c))
* **docker:** run with a safe sys.path so repo files cannot shadow our imports ([f232b14](https://github.com/Endika/specster/commit/f232b14956962f9425cbf1922514ea2720e7ed53))
* **llm:** reject base_url where the provider cannot use it ([6a5c498](https://github.com/Endika/specster/commit/6a5c498183e403ae9febe8299b864c5ec8a5884b))
* **llm:** surface OpenAI refusals and missing Azure keys ([d45cb02](https://github.com/Endika/specster/commit/d45cb02ed40711fed1d7234030d594cbd69ec077))
* **metrics:** trust only the last marker of a Specster comment and reject negative values ([62988b7](https://github.com/Endika/specster/commit/62988b744c7e04f3f9375a5b1e1a55bac8e0c1c6))
* **plan:** prefix mermaid node ids so task ids like end cannot break the graph ([8fcdcef](https://github.com/Endika/specster/commit/8fcdcef1d59282d5ca61da6e0e408f5411c2d2d8))
* **render:** cap the hidden content quoted in a comment and report the cut ([ce291cd](https://github.com/Endika/specster/commit/ce291cdee23db70902bc601fc063b38657c6c29d))
* **render:** escape model prose, clean code spans, show task descriptions and sanitize own comments ([37b1357](https://github.com/Endika/specster/commit/37b1357ca2c33345ef522c70a28a3e1bef7888a4))
* **render:** fence the mermaid plan so task titles cannot break it ([e91c890](https://github.com/Endika/specster/commit/e91c890b53253bdaa8109d399b9882970245d614))
* **render:** mark the files-read list when more than ten were read ([e049f8c](https://github.com/Endika/specster/commit/e049f8c851762dcc569c476f62467ea549504b63))
* **run:** bill the real cost when a label call fails after the reply ([30a623a](https://github.com/Endika/specster/commit/30a623ad884c81b81ef1641f46fa1555f65cfe2e))
* **run:** clear the trigger label even when the error comment cannot be posted ([7bc7dfd](https://github.com/Endika/specster/commit/7bc7dfda2e43eedd6a165cd17de9eb6339555239))
* **run:** remove the ready label when a run ends in questions ([41bab64](https://github.com/Endika/specster/commit/41bab643c85c97037cefe8a56820525e1f577410))
* **run:** write outcome=skipped for events that are not for Specster ([ae1f635](https://github.com/Endika/specster/commit/ae1f635b0e888973062ac7ba5476b1846e99e758))
* **sanitize:** strip every format character, tag characters and variation selectors, and sanitize the title ([6155166](https://github.com/Endika/specster/commit/6155166839263a986db44b47463b6eb14d4a8b7a))
* **sanitize:** write invisible character ranges as ASCII escapes ([7f7ef38](https://github.com/Endika/specster/commit/7f7ef3860e6c2ed8bdd3eecb1a57c320020229ff))
* **skills:** confine skill files to the repository and tolerate bad frontmatter ([ff7e126](https://github.com/Endika/specster/commit/ff7e1265cdeefe2257d47852ed2e14bec513a6d9))
* **thread:** defuse framing tags inside thread bodies ([819c7f4](https://github.com/Endika/specster/commit/819c7f4ebe347c001bf08e7cb2296f21e5b44746))
* **thread:** frame the thread with a per-run nonce instead of filtering tags ([542311e](https://github.com/Endika/specster/commit/542311e3d6fc428461e22e2293b3908e9a0961a3))
* **thread:** keep the hidden content Specster quoted out of the next round ([b3280ec](https://github.com/Endika/specster/commit/b3280ecf88f87441ea7fc76e06770cd8ff5790c4))
* **thread:** trust the issue author's answers by default with trust.issue_author ([979b741](https://github.com/Endika/specster/commit/979b7417bd19d55421454eb55b4193fe8b15abc9))
* **workspace:** cap list_dir at 500 entries and report the cut ([23b5a3a](https://github.com/Endika/specster/commit/23b5a3a7a446d2c55d53d7bf55632677d2da3a8f))
* **workspace:** hide gha-creds files from the agent and point Google credentials at the mounted copy ([5a9bd31](https://github.com/Endika/specster/commit/5a9bd31b6e75cb4f27dd799ebe70ff99f71b3882))
* **workspace:** report cut lines and skip oversized files ([bb6ee87](https://github.com/Endika/specster/commit/bb6ee87df1451f124949c576cf19eac7ccfb7d5e))


### Documentation

* count four api key inputs and note that renaming labels.spec needs the job if too ([aa778bc](https://github.com/Endika/specster/commit/aa778bc9eb12d63a675a693f8f18d62596fc899c))
* say plainly that keyless Azure needs service-principal env vars in v0.1 ([7294bd7](https://github.com/Endika/specster/commit/7294bd7424182e1bdea51ed593ffdb907db0fffa))
* write the README ([ff39add](https://github.com/Endika/specster/commit/ff39add160be6aa973a6f489f0afb69e7911c159))
