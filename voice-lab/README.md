# Spoken aichatcore RSI laboratory

This module runs audible, synthetic two-sided account-support conversations through
the **original aichatcore Java Playbook chain**. It imports selected Java sources from
a separately supplied checkout. It does not deploy the original enterprise Spring
application or connect to its database, Redis, algorithm service, or telephone system.

Each turn goes through:

`customer model → local Chinese TTS → telephone-band audio → Vosk ASR → original
Java PreCheck/PromptBuild/LLMCall/PostCheck → assistant TTS → audio → ASR → customer`

Only the recognition result reaches the other participant. The ground-truth transcript
is retained for independent error measurement; it is not supplied as ASR hints.
The speech worker writes audio files, uses no microphone and makes no outbound calls.
Calls are assembled from actual generated utterances with measured processing gaps;
they are not recordings of human participants. Microsoft SAPI voices are synthetic.

## What RSI optimizes

The editable bundle contains a dynamic Playbook prompt and bounded speaking rate.
The fixed evaluator measures service handling, character recognition error, excessive
uninterrupted speech, repeated answers and unsafe/preference-violating output.
These are engineering proxies, **not evidence of real customer comfort or repayment**.
The customer and prompt generator currently share a local Qwen model, so role bias
has not been excluded. Jev/SystemOne provides structured simulated operator reviews;
an AI review must never be represented as human sign-off.

Before proposing, training conversations are executed and retained. Twenty-four
different held-out customer cases are preregistered and run on both parent and candidate.
Promotion requires at least 10% lower quality loss, no metric regression, no unsafe
candidate run and a paired sign-test gate with alpha spending. Candidate code cannot
alter the executor, verifier, scenarios or evaluator. Bamboo's existing `rsi-sdk`
performs reproducible builds, evidence verification, promotion and parent inheritance.
Rejections and failures remain in the ledger. This is an offline benchmark release,
not a production/customer-facing release.

## Local setup

Build with Java 17 and Maven using the upstream checkout already supplied by the user:

```powershell
mvn -f voice-lab/java/pom.xml -Daichatcore.source=D:/path/to/aichatcore compile dependency:copy-dependencies
```

No enterprise Maven parent is used. Dependencies are pinned in `java/pom.xml`.
Upstream code, compiled classes, dependencies, model weights and recordings are not
redistributed with this module. The harness's code is MIT; this does not relicense
upstream Java code, SAPI voices, model weights or Maven dependencies. Keep `target/`
private: the upstream repository may embed legacy configuration in its class files.
The harness clears the upstream credential configuration and never initializes its
corporate client. Obtain the original service's proper runtime configuration before
any separate enterprise deployment.

Install `rsi-core`, `rsi-sdk`, and this package in an isolated Python 3.12 environment.
The runtime config supplies paths to Java, compiled classes, dependencies, the
PowerShell speech worker, ffmpeg and Vosk's `vosk-model-small-cn-0.22`, plus a loopback
OpenAI-compatible completion endpoint and the configured private Jev endpoint.
It also supplies an append-only shared model budget and a Java build manifest.
`tools/prepare.py --help` creates this config and manifest from explicitly supplied
local dependencies. Its default budget starts at zero for a new installation;
`--inherited-requests` and `--request-limit` preserve the cumulative bound when
continuing earlier experiments. The Jev deadline is configurable from 1 to 180 seconds.

```powershell
python -m bamboo_voice.campaign --config <private-config.json> --output <new-private-directory> --batches 2
```

Keep configuration, budgets, original waveforms, model requests, partial failures and
review records in ignored local storage. No customer data or credentials belong in Git.
The runner fails closed after three consecutive model failures and counts failed
attempts against its inherited budget. Start a new frozen campaign after a code fix;
never edit an executing campaign's evaluator or erase a failed trial.
Training progress is saved after every call, before any operator request. A failed
review remains pending and cannot be treated as approval. Existing frozen experiments
must be audited with their archived source version after an implementation update.

Review `conversation.json` together with `call.wav`, the raw sender/receiver WAVs,
`workflow/jobs/*/evaluation.json`, and `workflow/active.json`. A second batch explicitly
loads the current accepted version; inheritance is only proven if the first one
actually passed. The original game experiments and their acceptance gates are unchanged.

## Validation

`python -m pytest voice-lab/tests` covers audio-only handoff, budget inheritance and
failure consumption, train/evaluation separation, speaking-rate bounds and fixed
promotion criteria. An integration smoke requires the real Java and voice workers.
Passing unit tests is never substituted for audio execution or held-out evaluation.
