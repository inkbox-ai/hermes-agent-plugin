# Live CI coverage

## Full stack e2e

### Channels suite

**Proves:** Both channel-model legs complete successfully. **Flow:** 1. Run the channels action. 2. Require success before continuing.

### Agent2Agent suite

**Proves:** All five Agent2Agent scenarios complete successfully. **Flow:** 1. Run the scenarios serially. 2. Require success before continuing.

### Voice suite

**Proves:** Each selected voice scenario completes successfully. **Flow:** 1. End matching calls left by interrupted validations. 2. Run the voice matrix serially. 3. Require success before continuing.

### External-events suite

**Proves:** Authenticated event acceptance and invalid-event rejection complete successfully. **Flow:** 1. Run the external-event action. 2. Require success before continuing.

### Aggregate gate

**Proves:** Every delegated live suite reported success. **Flow:** 1. Read each suite result. 2. Pass only when all results are successful.

## Live — Agent2Agent

### Inbound single-turn

**Proves:** The agent completes a remotely opened task with the requested result. **Flow:** 1. Open a tagged task. 2. Wait for completion. 3. Check its result marker.

### Inbound multi-turn

**Proves:** The agent requests caller input before completing the task. **Flow:** 1. Open a task. 2. Answer its input request. 3. Check the final history and result.

### Inbound long-running progress

**Proves:** A long-running worker turn promptly acknowledges receipt, reports its configured cadence, sends two periodic updates, and returns the expected result. **Flow:** 1. Open a two-minute arithmetic task. 2. Check the receipt. 3. Wait for completion. 4. Check ordered progress and the final result.

### Outbound single-turn

**Proves:** The agent delegates work and waits for the worker before completing. **Flow:** 1. Request delegation. 2. Complete the worker task. 3. Check the outer result.

### Outbound multi-turn

**Proves:** The agent handles worker-requested input during delegation. **Flow:** 1. Request delegation. 2. Answer the worker. 3. Complete it. 4. Check the outer result.

## Live — agent channels (email + SMS)

### Email reachability

**Proves:** The deterministic model receives email and sends a correlated reply. **Flow:** 1. Send unique email. 2. Wait for its reply marker. 3. Mock leg only; real leg skips it.

### Email basic reply

**Proves:** The real model returns a non-empty email reply. **Flow:** 1. Send an acknowledgement request. 2. Wait for a correlated response. 3. Real leg only.

### Email reports own identity

**Proves:** The real model can report its configured identity fields. **Flow:** 1. Ask for identity details. 2. Compare the response with current product data. 3. Real leg only.

### Email reports sender name

**Proves:** The real model can resolve the sender's synthetic contact without disclosing private fields. **Flow:** 1. Read the seeded contact fixture. 2. Ask for its name and stored contact-method kinds with a per-run reference. 3. Require the stored name, both method kinds, and current reference in the correlated reply. 4. Real leg only.

### Email names available tools

**Proves:** The real model sees the expected contact-tool surface. **Flow:** 1. Ask for tool names. 2. Require the supported contact tools. 3. Real leg only.

### Email contact lifecycle

**Proves:** Contact create, update, and delete tools work when mutation is explicitly enabled. **Flow:** 1. Collect the test in both legs. 2. Skip it in the standard action. 3. Run only with separate mutation opt-in.

### SMS reachability

**Proves:** The deterministic model receives SMS and returns the expected marker. **Flow:** 1. Send a unique prompt. 2. Wait for the correlated reply. 3. Mock leg only; real leg skips it.

### SMS basic reply

**Proves:** The real model returns a non-empty SMS reply. **Flow:** 1. Send an acknowledgement request. 2. Wait for the correlated response. 3. Real leg only.

### SMS reports own identity

**Proves:** The real model can report its configured email identity. **Flow:** 1. Ask for identity details. 2. Require the current email in the reply. 3. Real leg only.

### SMS reports sender details

**Proves:** The real model can report a known sender's contact name. **Flow:** 1. Look up the sender. 2. Ask who sent the SMS. 3. Require the stored name when present. 4. Real leg only; skips without a contact match.

### SMS contact lookup

**Proves:** The real model can use contact tools from SMS. **Flow:** 1. Create a unique contact. 2. Ask the agent to find it. 3. Require its stored name. 4. Remove the fixture. 5. Real leg only.

### SMS recovery after asynchronous failure

**Proves:** An authenticated delivery-failure event wakes the agent and produces a fresh follow-up. **Flow:** 1. Establish the conversation. 2. Submit the failure event. 3. Observe a new inbound SMS and recovery evidence. 4. Real leg only.

### SMS synchronous block feedback

**Proves:** A synchronous content rejection reaches the agent's turn. **Flow:** 1. Request content expected to be rejected. 2. Accept either supported send path. 3. Require rejection feedback evidence. 4. Real leg only; a follow-up is optional.

### Email-to-SMS

**Proves:** An email request can produce an SMS containing the run marker. **Flow:** 1. Send email with a unique marker. 2. Wait for a new SMS carrying it. 3. Real leg only.

### SMS-to-email

**Proves:** An SMS request can produce an email containing the run marker. **Flow:** 1. Send SMS with a unique marker. 2. Wait for a new email carrying it. 3. Real leg only.

### Email-to-call

**Proves:** An email request creates paired call records with voicemail detection disabled. **Flow:** 1. Snapshot both call owners. 2. Request a call by email. 3. Correlate the fresh pair and inspect the outbound policy. 4. Real leg only.

### SMS-to-call

**Proves:** An SMS request creates paired call records with voicemail detection disabled. **Flow:** 1. Snapshot both call owners. 2. Request a call by SMS. 3. Correlate the fresh pair and inspect the outbound policy. 4. Real leg only.

### Inbound TTS/STT voice test

**Proves:** Nothing in the channels action; this scenario belongs to the voice matrix. **Flow:** 1. Collect the test. 2. Skip it because no voice scenario is selected.

### Outbound Voice AI test

**Proves:** Nothing in the channels action; this scenario belongs to the voice matrix. **Flow:** 1. Collect the test. 2. Skip it because no voice scenario is selected.

### Outbound realtime contact test

**Proves:** Nothing in the channels action; this scenario belongs to the voice matrix. **Flow:** 1. Collect the test. 2. Skip it because no voice scenario is selected.

### Outbound realtime test

**Proves:** Nothing in the channels action; this scenario belongs to the voice matrix. **Flow:** 1. Collect the test. 2. Skip it because no voice scenario is selected.

### Authenticated product external event

**Proves:** Nothing in the channels action; required external-event inputs are absent. **Flow:** 1. Collect the test. 2. Skip it in both channel legs.

### Invalid external authentication

**Proves:** Nothing in the channels action; required external-event inputs are absent. **Flow:** 1. Collect the test. 2. Skip it in both channel legs.

### Valid external authentication

**Proves:** Nothing in the channels action; required external-event inputs are absent. **Flow:** 1. Collect the test. 2. Skip it in both channel legs.

## Live — voice calls (Inkbox Voice AI + TTS/STT + realtime)

### Inbound TTS/STT

**Proves:** The TTS/STT stack holds a two-way call and persists disabled voicemail detection. **Flow:** 1. Place an inbound call. 2. Require speech from both parties. 3. Inspect call policy and speech mode. 4. Run only when inbound coverage is selected.

### Outbound Voice AI

**Proves:** Voice AI records caller intent and an open action, emits completion evidence, and creates a current marker-bearing SMS to the caller. **Flow:** 1. Request a hosted callback. 2. Check mode, reason, authority, and voicemail policy. 3. Wait for transcript, action, completion, and matching SMS evidence.

### Outbound realtime contact lookup

**Proves:** The realtime stack can consult a directly read contact and speak its requested details during a two-way call. **Flow:** 1. Seed a contact. 2. Request a callback about it. 3. Correlate the call, contact-read evidence, and persisted AUT speech. 4. Inspect speech mode and voicemail policy.

### Outbound realtime

**Proves:** The realtime stack places a callback, holds a two-way call, and persists disabled voicemail detection. **Flow:** 1. Request a callback by SMS. 2. Correlate both call records. 3. Check conversation and speech mode.

## Live — external events

### Authenticated product external event

**Proves:** A valid authenticated event is accepted for agent handling. **Flow:** 1. Submit a uniquely identified event. 2. Require acceptance. 3. Wait for its enqueue evidence.

### Invalid external authentication

**Proves:** Invalid external authentication is rejected without waking the agent. **Flow:** 1. Submit an invalid event. 2. Require rejection. 3. Confirm no matching wake occurs.

### Valid external authentication

**Proves:** Valid external authentication reaches agent intake. **Flow:** 1. Submit a valid event. 2. Require acceptance. 3. Wait for its enqueue evidence.
