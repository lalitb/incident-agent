# Two runs to read

These are sanitized copies of actual historical model runs. Their conclusions
and original success/failure outcomes have not been rewritten.
Their schema differs from the current structured-measurement contract. Read the
linked files as a walkthrough; do not expect current report validation to accept
an old schema. New reports belong in `runs/`.

## Successful execution, incomplete investigation

[Controller](successful/controller.json),
[evidence](successful/evidence.json),
[report](successful/report.json).

Kimi K3 made five planning calls, six telemetry calls, and one final report call.
The controller rejected a decision that changed an outstanding verification
target. No tool in that rejected decision executed; the next decision recovered.
Collection and reporting completed against retained September 20 telemetry.

The model proposed pool queuing as the explanation, with medium confidence.
It retrieved no traces and left two tool calls unused. Acquisition versus query
execution was not directly verified. Some causal and timing prose went beyond
what the measurements established. Completion is not proof of a correct diagnosis.

## Provider failure with saved progress

[Controller](provider-failure/controller.json),
[evidence](provider-failure/evidence.json).

The first Gemini planning request failed with a reported daily quota error.
The model never supplied a usable decision. The code recorded `provider_error`,
kept the run files, and did not request a final report or invent a diagnosis.
This run demonstrates a provider boundary, not model reasoning quality.

A separate [reporting failure](report-failure/controller.json) shows collection
completing but reporting failing under the older prose-validation contract.
Its [rejected report](report-failure/report_rejected.json) and
[evidence](report-failure/evidence.json) are retained for inspection, not relabeled
as a success. The [experiment ground truth](ground_truth.json) records the
controlled conditions separately from the model inputs.
