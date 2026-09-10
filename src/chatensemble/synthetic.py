"""Synthetic threads for a few fake accounts.

Used by ``chatensemble --synthetic`` to exercise the multi-account thread browser
without a real server. Each account has its own *persona* -- its own people,
channel-naming scheme and topics -- so switching tabs visibly changes the
content. Deterministic for a given ``seed``.
"""

from __future__ import annotations

import random
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import ChannelKind, Message

_REACTION_POOL = ["+1", "eyes", "rocket", "tada", "fire", "pray", "white_check_mark",
                  "thinking_face", "100", "raised_hands"]

# reply-count distribution: many small threads, a few big ones
_REPLY_WEIGHTS = [(0, 6), (1, 9), (2, 8), (3, 6), (5, 4), (8, 2), (13, 1)]


# --------------------------------------------------------------------------- #
# personas -- one per fake account, deliberately distinct
# --------------------------------------------------------------------------- #
@dataclass(slots=True, frozen=True)
class Persona:
    provider: str
    people: tuple[tuple[str, str], ...]
    channels: tuple[tuple[str, ChannelKind], ...]
    openers: tuple[str, ...]
    replies: tuple[str, ...]


_PUB = ChannelKind.PUBLIC
_PRIV = ChannelKind.PRIVATE

_HELMHOLTZ = Persona(
    provider="mattermost",
    people=(
        ("hz_klaus", "Klaus Bergmann"), ("hz_marta", "Marta Reinholt"),
        ("hz_pavel", "Pavel Novak"), ("hz_ana", "Ana Ferreira"),
        ("hz_sven", "Sven Ohlsson"), ("hz_lena", "Lena Hartmann"),
        ("hz_deniz", "Deniz Yilmaz"), ("hz_greg", "Greg Whitlock"),
        ("hz_ines", "Ines Cabrera"), ("hz_ravi", "Ravi Menon"),
    ),
    channels=(
        ("eng-training", _PUB), ("eng-infra", _PUB), ("eng-eval", _PUB),
        ("research", _PUB), ("incidents", _PRIV), ("random", _PUB),
    ),
    openers=(
        "@channel the 13B run OOM'd at step 12k -- bumping activation checkpointing "
        "and restarting from the 11k snapshot.",
        "GPU queue on the A100 partition is backed up ~14h. If your job isn't "
        "critical for the Friday review, please hold.",
        "Scaling-law sweep for the 300M-3B range is done. Compute-optimal tokens "
        "land ~20x params in our setup; plots in the doc.",
        "Anyone else seeing NCCL timeouts on node group 7 since the driver update? "
        "Third job killed this morning.",
        "Proposing we freeze the tokenizer at v4 for all runs this quarter -- "
        "retraining it mid-cycle keeps invalidating comparisons.",
        "Checkpoint storage is at 89%. I'll prune intermediate checkpoints older "
        "than 30 days unless someone objects.",
        "The eval harness now caches tokenized datasets -- full suite dropped from "
        "40min to 9min per checkpoint.",
        "Reminder: weekly training sync moved to Thursdays 10:00 CET. Agenda doc "
        "is open for items.",
        "Loss spikes look correlated with data shard 0043. Pulling it from the mix "
        "and re-shuffling.",
        "Draft of the infra postmortem for last week's cluster outage is ready for "
        "review -- comments by EOD please.",
        "Do we want to try FP8 for the next run? Transformer Engine looks stable "
        "now and it'd buy ~1.3x throughput.",
        "Kicking off the 7B long-context extension (32k) tonight. Will watch the "
        "rope scaling and report tomorrow.",
    ),
    replies=(
        "Restarted fine on my end, throughput back to ~118 TFLOP/s.",
        "Same NCCL issue here -- downgrading the driver on node 7 fixed it.",
        "+1 on freezing the tokenizer, the churn isn't worth it.",
        "I object to pruning the 25k checkpoint, we still need it for the ablation.",
        "The eval speedup is huge for the babysitting loop, nice.",
        "Added two items to the sync agenda.",
        "Confirmed shard 0043 is corrupt -- bad decode on ~2% of records.",
        "Left comments on the postmortem, mostly wording.",
        "FP8 gave me NaNs last month but that was an older TE build, worth retrying.",
        "Long-context run is at 8k now with stable loss, looks good.",
        "Can we get a dashboard for the queue backlog? Hard to plan otherwise.",
        "Storage cleared to 72%, pruned ~40 old checkpoints.",
    ),
)

_OPENGPT_X = Persona(
    provider="slack",
    people=(
        ("ox_bea", "Beatriz Lobo"), ("ox_henrik", "Henrik Sorensen"),
        ("ox_carla", "Carla Meier"), ("ox_tomek", "Tomasz Wojcik"),
        ("ox_ilse", "Ilse van Dijk"), ("ox_matteo", "Matteo Bruno"),
        ("ox_agnes", "Agnes Kovacs"), ("ox_ruben", "Ruben Ortiz"),
        ("ox_petra", "Petra Horakova"), ("ox_johan", "Johan Lindqvist"),
    ),
    channels=(
        ("T4.2-model-exploration", _PUB), ("T4.3-data-composition", _PUB),
        ("T4.5-evaluation", _PUB), ("T5.4-multilingual", _PUB),
        ("wp4-moe", _PRIV), ("coordination", _PUB),
    ),
    openers=(
        "Hi @channel, T4.3 biweekly is today at 13:00 CEST. Please add agenda "
        "items -- we need to close the data-mix decision.",
        "The multilingual eval set now covers 24 languages. Gap: still missing a "
        "good Slovene reading-comprehension task.",
        "For deliverable D4.2, do we report the 1.4B ablations or wait for the 7B "
        "numbers? Deadline is the 15th.",
        "Licensing check on the Common Crawl subset came back clean for "
        "redistribution. Doc updated.",
        "Proposal: a shared W&B team so partners can see each other's runs without "
        "screenshotting into Slack.",
        "Who owns the tokenizer fertility comparison across languages? It's "
        "blocking the T5.4 section.",
        "The MoE 8x7B config is training. Router load-balancing loss is behaving, "
        "expert utilization ~0.9.",
        "We need a decision on eval cadence: every checkpoint is too expensive, "
        "every 5k steps misses regressions.",
        "Reminder to log your person-months for the Q3 report -- template is in "
        "the coordination channel.",
        "Compute allocation for October: LUMI 40%, Leonardo 35%, JUWELS 25%. "
        "Objections by Friday.",
        "Polish and Czech shards re-tokenized with v3. Comparability note added to "
        "the ablation table.",
        "Can we align on a single checkpoint format? Three partners, three "
        "conventions, endless conversion scripts.",
    ),
    replies=(
        "Added the data-mix item to the agenda.",
        "I can take the Slovene task, I have a contact at the university there.",
        "Report the 1.4B numbers, footnote that 7B is in progress.",
        "Shared W&B team makes sense, I'll set it up.",
        "I own the fertility comparison -- draft by Wednesday.",
        "Expert utilization looks healthy.",
        "Vote for every 5k steps plus a full eval at each 50k.",
        "Logged my person-months.",
        "No objection to the compute split.",
        "v3 re-tokenization confirmed on my side too.",
        "+1 for a single checkpoint format, this is death by conversion script.",
        "Licensing doc looks good, thanks for chasing it.",
    ),
)

_TROVE_AI = Persona(
    provider="slack",
    people=(
        ("tv_sam", "sam"), ("tv_priya", "Priya Nair"), ("tv_deno", "deno"),
        ("tv_kai", "Kai Lund"), ("tv_rosa", "Rosa Imai"),
        ("tv_marco", "Marco Duarte"), ("tv_lily", "Lily Chen"),
        ("tv_ben", "Ben Osei"), ("tv_nadia", "Nadia Haddad"),
        ("tv_theo", "Theo Marchetti"),
    ),
    channels=(
        ("product", _PUB), ("eng", _PUB), ("ml", _PUB),
        ("customers", _PRIV), ("oncall", _PRIV), ("general", _PUB),
    ),
    openers=(
        "Shipping the new retrieval reranker to prod at 15:00. Rollback plan is in "
        "the runbook, ping me if latency p99 spikes.",
        "Customer ACME is hitting the 429 rate limit during their nightly batch. "
        "Can we bump their tier temporarily?",
        "The eval set for the summarization feature is stale -- half the examples "
        "predate the schema change.",
        "Standup async: yesterday shipped the export API, today onboarding docs, "
        "no blockers.",
        "We're burning ~$4k/day on the 70B endpoint at 12% utilization. Proposing "
        "we route low-priority traffic to the 8B.",
        "Incident postmortem: the 02:00 outage was a bad deploy that skipped the "
        "migration step. Action items in the doc.",
        "Do we support streaming for the new chat endpoint at launch, or "
        "fast-follow? Sales is asking.",
        "Prompt injection got past the input filter in a customer demo. Patched, "
        "but we need real red-teaming before GA.",
        "Model swap: moving the default from v2.3 to v2.5. Quality up on our "
        "internal set, latency flat.",
        "Can someone review the pricing page copy? Legal wants the token-usage "
        "disclaimer reworded.",
        "The fine-tune job for customer BETA finished -- eval numbers look good, "
        "sending them checkpoint access.",
        "Oncall handoff: one open page (elevated error rate on embeddings, "
        "mitigated), watch the retry queue.",
    ),
    replies=(
        "Reranker is live, p99 looks flat.",
        "Bumped ACME to the next tier for 7 days, told their account manager.",
        "I'll regenerate the summarization eval set today.",
        "No blockers from me either.",
        "+1 on routing low-priority traffic to the 8B, the cost is silly.",
        "Added the migration-check step to the deploy script so this can't recur.",
        "Streaming should be fast-follow, launch without it.",
        "Red-teaming: I can scope a pass this sprint.",
        "v2.5 swap looks safe from the eval, ship it.",
        "Reworded the disclaimer, legal signed off.",
        "Sent BETA their checkpoint access.",
        "Retry queue drained, error rate back to normal.",
    ),
)

PERSONAS: dict[str, Persona] = {
    "helmholtz": _HELMHOLTZ,
    "opengpt-x": _OPENGPT_X,
    "trove-ai": _TROVE_AI,
}
DEFAULT_ACCOUNTS: tuple[str, ...] = tuple(PERSONAS)


def _persona_for(name: str) -> Persona:
    if name in PERSONAS:
        return PERSONAS[name]
    keys = list(PERSONAS)
    return PERSONAS[keys[zlib.crc32(name.encode()) % len(keys)]]


def _rng(*parts: object) -> random.Random:
    key = "|".join(str(p) for p in parts)
    return random.Random(zlib.crc32(key.encode()))


# --------------------------------------------------------------------------- #
# generation
# --------------------------------------------------------------------------- #
def _weighted_reply_count(rng: random.Random) -> int:
    pop = [n for n, w in _REPLY_WEIGHTS for _ in range(w)]
    return rng.choice(pop)


def _one_account(
    account: str,
    persona: Persona,
    *,
    rng: random.Random,
    n_threads: int,
    days: int,
    now: datetime,
) -> list[Message]:
    people = list(persona.people)
    channels = list(persona.channels)
    provider = persona.provider
    me_id, me_name = rng.choice(people)  # the "own" identity for this account

    out: list[Message] = []
    for ti in range(n_threads):
        chan_name, chan_kind = rng.choice(channels)
        chan_id = f"{account}-{zlib.crc32(chan_name.encode()) % 9973:04d}"
        root_author = rng.choice(people)

        span = days * 24 * 60
        root_ts = now - timedelta(minutes=rng.randint(30, span))
        opener = rng.choice(persona.openers)
        if rng.random() < 0.18:
            opener = "(!) " + opener
        n_replies = _weighted_reply_count(rng)

        thread_id = f"{account}:{ti:03d}:0"
        root = Message(
            id=thread_id,
            text=opener,
            timestamp=root_ts,
            author_id=root_author[0],
            author_name=root_author[1],
            channel_id=chan_id,
            channel_name=chan_name,
            channel_kind=chan_kind,
            account=account,
            provider=provider,
            thread_id=thread_id,
            reply_count=n_replies,
            is_own=root_author[0] == me_id,
            permalink=f"https://{provider}.example/{account}/{thread_id}",
        )
        if rng.random() < 0.3:
            root.reactions = {
                r: rng.randint(1, 5)
                for r in rng.sample(_REACTION_POOL, k=rng.randint(1, 2))
            }
        msgs = [root]

        ts = root_ts
        for k in range(1, n_replies + 1):
            ts = min(ts + timedelta(minutes=rng.randint(2, 240)), now - timedelta(minutes=1))
            a = rng.choice(people)
            r = Message(
                id=f"{account}:{ti:03d}:{k}",
                text=rng.choice(persona.replies),
                timestamp=ts,
                author_id=a[0],
                author_name=a[1],
                channel_id=chan_id,
                channel_name=chan_name,
                channel_kind=chan_kind,
                account=account,
                provider=provider,
                thread_id=thread_id,
                is_own=a[0] == me_id,
                permalink=f"https://{provider}.example/{account}/{thread_id}",
            )
            if rng.random() < 0.18:
                r.reactions = {rng.choice(_REACTION_POOL): rng.randint(1, 3)}
            msgs.append(r)

        # unread / mention bookkeeping: some threads fully unread, some with just
        # a fresh tail, a few carrying an @mention of "me".
        roll = rng.random()
        if roll < 0.4:
            tail = rng.randint(1, max(1, len(msgs)))
            for m in msgs[-tail:]:
                if not m.is_own:
                    m.is_unread = True
        if roll < 0.12:
            target = rng.choice(msgs)
            if not target.is_own:
                target.is_mention = True
                target.is_unread = True
                if not target.text.startswith(("@", "(!)")):
                    target.text = f"@{me_name.split()[0].lower()} " + target.text

        out.extend(msgs)
    return out


def synthetic_messages(
    accounts: Sequence[str] | None = None,
    *,
    seed: int = 0,
    threads_per_account: int = 14,
    days: int = 21,
    now: datetime | None = None,
) -> list[Message]:
    """Generate threaded :class:`Message` objects across several fake accounts.

    ``accounts`` defaults to :data:`DEFAULT_ACCOUNTS`. A name in :data:`PERSONAS`
    uses that persona; any other name is mapped to one of the personas
    deterministically. Output is deterministic for a given ``seed``.
    """
    now = now or datetime.now(timezone.utc)
    names = tuple(accounts) if accounts is not None else DEFAULT_ACCOUNTS

    out: list[Message] = []
    for name in names:
        out.extend(
            _one_account(
                name, _persona_for(name),
                rng=_rng(seed, name),
                n_threads=threads_per_account, days=days, now=now,
            )
        )
    out.sort(key=Message.sort_key)
    return out
