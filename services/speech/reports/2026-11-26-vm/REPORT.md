# Real uploaded recordings on the 4 GB VM — 2026-11-26

Measured on the existing `metis` e2-medium VM (2 shared vCPU, 3908 MiB
visible RAM), Linux 7.0.0-1018-gcp x86_64 / glibc 2.39. This is a small pilot
on existing uploaded WebM recordings, not synthetic TTS. No audio, transcript,
object key or user identifier is included in these artifacts.

The baseline called the existing authenticated loopback speech server through
`RemoteTranscriber`, so it reused its one warm model and did not create another
Whisper process. These measurements precede the new resource limits/2-thread
override and do not establish five-minute-clip or concurrent-upload performance.

Model: `small.en`, artifact `d1d751a5f8271d482d14ca55d9e2deeebbae577f`,
faster-whisper 1.0.3, CPU int8, beam 1, temperature 0, VAD enabled, language en.
The existing service did not report its effective CPU thread count. Three
content hashes were distinct among six uploaded files; duplicate bytes were
not counted as additional recordings.

| Anonymous clip | Audio seconds | HTTP wall seconds | Decode seconds |
| --- | ---: | ---: | ---: |
| 1 | 17.629 | 11.939 | 10.641 |
| 2 | 8.759 | 12.293 | 12.205 |
| 3 | 21.119 | 10.427 | 10.208 |
| Total | 47.507 | 34.659 | 33.054 |

Aggregate real-time factor was **0.7296** (wall seconds / audio seconds).
Clip 1 reported `cold_start=true`, meaning the first decode in that already
loaded service, not a cold machine/model load. Later clips reported false.

Afterward, the speech service reported a lifetime peak of 1,230,495,744 bytes
(~1173.5 MiB) and current memory 897,024,000 bytes. Its proposed 1536 MiB
resident cap has headroom above this observed short-clip peak, but longer and
concurrent workloads still require measurement. Swap is overflow for memory
pressure; it does not guarantee protection from service caps or a full disk.

One audio source had an existing user-edited transcript: a 145-character ASR
revision followed by a 146-character user revision. We decoded that recording
again and compared in memory against the user revision: **WER 4%**, one clip,
17.629 s audio, 9.712 s HTTP wall, 8.667 s decode. This is a user-correction
pilot, not an independently annotated ground-truth set. Name/date/negation
fidelity are **unmeasured** because this reference had no annotated names or
recognized date/negation examples. No reference or hypothesis is saved here.

The numbers above were captured with read-only diagnostic calls against the
existing VM deployment. The integrated reproducible command is:

```bash
# Run with the protected service env loaded, ENGINE=remote and loopback endpoint.
make speech-bench SPEECH_BENCH_ARGS="--audio-dir /var/lib/metis/objects --speed-only --passes 2 --report /tmp/speech-bench.json"
```

For a proper accuracy set, supply independently checked human references via
`--fixtures` and omit `--speed-only`; reference IDs must match the unique audio
filenames. The default `make speech-bench` reference echo measures only the
scoring code. The new real-audio path refuses the fixture transcriber.
