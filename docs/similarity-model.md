# Bot play-style similarity model

Reference for the play-style similarity calculation (`web/similarity.py`). Given
two bots $a$ and $b$, it produces three numbers:

| output | symbol | what it is |
|---|---|---|
| style correlation | $\hat\rho$ | the estimate — do the two deviate from expectation against the same opponents |
| posterior spread | $\hat\sigma_\rho$ | how tightly $\hat\rho$ is pinned down |
| ranking statistic | $P(\rho > 0.5)$ | posterior mass above a threshold; large correlation *and* enough evidence |

The model uses **win rates only**. Everything is derived from per-opponent
win/loss counts and ELO ratings, and nothing else.

This document covers the calculation. How the page draws it — colour scales,
fading, column layout — is not documented here; see the comments in
`web/templates/similarity.html`.

---

## 0. What the model is actually asking

The naive question — *"do $a$ and $b$ have similar win rates?"* — is the wrong
one, because win rate is dominated by **strength**. Two bots of equal ELO have
similar win rates against everyone regardless of how they play. Measured on our
data, raw win-rate-vector correlation has correlation $-0.22$ with the ELO gap
between the pair; after the correction in step 2 that drops to $-0.02$.

The question the model asks instead is:

> Once we account for how strong $a$ is, how strong $b$ is, and how hard each
> opponent is, do $a$ and $b$ over- and under-perform against **the same
> opponents**?

That residual — the part of the result that the strengths do not explain — is
what "style" means here. Two bots running the same strategy beat the opponents
that strategy counters and lose to the opponents that counter it.

---

## Notation

| symbol | meaning |
|---|---|
| $a, b$ | the two bots being compared |
| $o$ | an opponent |
| $w_{ao}, l_{ao}$ | wins and losses of $a$ against $o$, within one competition |
| $R_a$ | $a$'s ELO in that competition |
| $\mathcal{O}_a$ | opponents $a$ has played |
| $\mathcal{O}_{ab} = \mathcal{O}_a \cap \mathcal{O}_b$ | common opponents |
| $\mathcal{A}_o$ | bots that have played $o$ |
| $\mathcal{C}$ | all populated $(a,o)$ cells |

Everything is scoped to a **single competition**: ELO, opponents and the map
pool all change between seasons. Cross-season comparison is a variant, see §8.

---

## 1. Per-cell log-odds and its sampling variance

**What.** For each (bot, opponent) cell, convert the win/loss record into
log-odds and attach the variance of that estimate.

$$
\tilde{w}_{ao} = w_{ao} + \tfrac{1}{2}, \qquad \tilde{l}_{ao} = l_{ao} + \tfrac{1}{2}
$$

$$
\hat\lambda_{ao} = \log\frac{\tilde{w}_{ao}}{\tilde{l}_{ao}}
\qquad\qquad
u_{ao} = \frac{1}{\tilde{w}_{ao}} + \frac{1}{\tilde{l}_{ao}}
$$

**Why log-odds.** Win rates live in $[0,1]$, so they compress near the ends: the
difference between 95% and 99% is small on that scale but large in strategic
terms. Log-odds is unbounded and additive, which makes the strength decomposition
in steps 2–3 a simple subtraction.

**Why the $+\tfrac{1}{2}$** (Haldane–Anscombe). A cell with 0 wins or 0 losses
gives infinite log-odds. Adding a half to each count keeps every cell finite and
shrinks small cells toward even, which is the right prior behaviour when we know
almost nothing about them.

**Why $u_{ao}$ matters more than it looks.** This is the delta-method variance of
$\hat\lambda_{ao}$, and it is how the model handles small samples. A cell built
on 3 games is not discarded — it arrives with a large $u$ and the likelihood in
step 6 down-weights it in proportion to what it actually knows, rather than
being cut by a blunt threshold.

**But a one-game cell is excluded outright, because its problem is bias, not
noise.** With the Haldane correction a single game gives
$\hat\lambda = \log(1.5/0.5) = \pm 1.10$ and nothing else — the value is capped
regardless of the opponent. Against a much stronger opponent the ELO expectation
in step 2 can be $-5.65$, so the residual comes out at roughly $+4.55$ *whether
the bot won or lost*. It measures the gap between what ELO demands and what one
game can express, not how the bot played. Weighting by $u$ cannot absorb that,
because $u$ describes variance around a correct centre and this centre is wrong.

The effect is systematic rather than random: a weak bot's thin cells are forced
positive and a strong bot's thin cells forced negative, both as a function of the
ELO gap, so two bots at opposite ends of the ladder acquire a shared component
that survives the double-centring of step 3. On real data this produced a
spurious $\hat\rho = +0.77$ between bots 1154 ELO apart, on 36 shared opponents
of which the weaker had played 35 exactly once and every one of the 36 cells was
saturated at zero wins or zero losses.

Hence `MIN_CELL_GAMES = 2`. Raising it further does not help: at 3 or 5 the
labelled positives are unchanged and coverage keeps falling, so 2 is the
smallest threshold that removes the artifact. It costs about a third of scored
pairs on a large season — pairs whose scores rested on single games.

**Ties are dropped.** Empirically it makes no difference — splitting them 50/50
moves AUC by 0.004 — and dropping them keeps an exact binomial. Note separately
that *tie rate* is a strong style property in its own right (0.000 to 0.221
across bots with $\ge 200$ games), so it is a candidate future dimension, but it
is not part of this model.

---

## 2. Remove the ELO expectation

**What.** Subtract the log-odds that ELO alone predicts.

ELO's implied win probability is a logistic,
$\Pr(a \text{ beats } o) = \left(1 + 10^{(R_o - R_a)/400}\right)^{-1}$, so on the
log-odds scale it is linear:

$$
C = \frac{\ln 10}{400} \approx 0.0057565
$$

$$
r_{ao} = \hat\lambda_{ao} - C\,(R_a - R_o)
$$

**Why.** ELO is a properly fitted, precision-weighted global estimate of strength
that already accounts for **schedule** — which matters because matchmaking pairs
bots of similar rating, so every bot faces a different opponent mix. A raw average
over a bot's own results would confound "how strong is this bot" with "how hard
were its opponents". ELO does that job well, so we use it rather than re-deriving
strength ourselves.

---

## 3. Double-centring

**What.** Iteratively remove the row (bot) and column (opponent) means:

$$
\gamma_o^{(t)} = \frac{1}{|\mathcal{A}_o|}\sum_{a \in \mathcal{A}_o} x_{ao}^{(t)},
\qquad x_{ao}^{(t+1/2)} = x_{ao}^{(t)} - \gamma_o^{(t)}
$$

$$
\alpha_a^{(t)} = \frac{1}{|\mathcal{O}_a|}\sum_{o \in \mathcal{O}_a} x_{ao}^{(t+1/2)},
\qquad x_{ao}^{(t+1)} = x_{ao}^{(t+1/2)} - \alpha_a^{(t)}
$$

starting from $x_{ao}^{(0)} = r_{ao}$ and running 8 passes. Write the converged
result $x_{ao}$.

**Why this is not redundant with step 2.** ELO is one parameter per bot, so it
cannot express *"this opponent is harder than its rating suggests"*. Real ladders
violate that constantly — counter-builds, crash-prone bots handing out free wins,
the logistic being the wrong shape at the tails. What survives step 2 is a
two-way decomposition:

$$
r_{ao} \;=\; \mu + \alpha_a + \gamma_o + x_{ao}
$$

where $\alpha_a$ is $a$'s leftover strength offset, $\gamma_o$ is a systematic
bias everyone shares against opponent $o$, and $x_{ao}$ is the interaction.
**Style is the interaction term.**

The main effects are not small. On our data they are 24.4% of the post-ELO
residual variance ($\mathrm{sd}(\alpha) = \mathrm{sd}(\gamma) = 0.661$ against a
total residual sd of 1.580), and $\gamma_o$ correlates $+0.50$ with opponent ELO
— a systematic curve, not style.

Leaving $\gamma_o$ in is actively harmful, because **correlation measures shared
variation and $\gamma_o$ appears in every bot's vector**:

$$
r_{ao} \approx \gamma_o + \mathrm{style}_{ao}, \qquad
  r_{bo} \approx \gamma_o + \mathrm{style}_{bo}
$$

Correlating those partly correlates $\gamma$ with itself — common mode, like
comparing two recordings that both carry the same mains hum. It is worst at the
ELO extremes where the misfit is largest. Before double-centring, the top-scoring
pair was a 1040-ELO bot against an 1839-ELO bot, and correlation with the ELO gap
was $+0.16$; afterwards it is $-0.06$.

**Why it iterates.** The matrix is incomplete and unbalanced, so removing row
means reintroduces column means. Convergence of the residual column means:

| passes | max abs. column mean |
|---|---|
| 1 | 0.868 |
| 2 | 0.070 |
| 4 | 0.0035 |
| 8 | 1.1 × 10⁻⁵ |

One pass leaves a column effect of 0.87 log-odds — comparable to the style signal
itself. Hence a loop, not a single subtraction.

---

## 4. Pooled style variance

**What.** Estimate one style variance for the whole ladder, by method of moments
over every populated cell:

$$
\hat\sigma^2 = \max\left(
\underbrace{\frac{1}{|\mathcal{C}|}\sum_{(a,o) \in \mathcal{C}} x_{ao}^2}_{\text{observed spread}}
\;-\;
\underbrace{\frac{1}{|\mathcal{C}|}\sum_{(a,o) \in \mathcal{C}} u_{ao}}_{\text{mean sampling noise}}
,\;\; \sigma^2_{\min}\right)
$$

with $\sigma^2_{\min} = 0.05$, which never binds in practice. Observed variance is
true variance plus noise variance, so the noise is subtracted off. ($x$ is
already centred, so the mean of squares is the variance.)

**Why pooled and not per bot.** A single bot's estimate is built from its own
50–130 opponents and is too noisy to use directly — it still comes out negative
for about 12% of bots, because cell noise is comparable to the style signal.
Pooling across every cell in the season removes that instability.

**Partial pooling was implemented, measured, and reverted.** Shrinking each bot's
own estimate toward the ladder's, weighted by opponent count, is the textbook
answer and it is better motivated than full pooling. On real data it moved AUC by
$+0.007$ on competition 36 and $+0.001$ on competition 35, with the labelled
cases split — the confirmed rewrite slightly better, the suspected case and both
negative controls slightly worse. That does not earn a tuned shrinkage constant.

**Open limitation: Random wrappers.** A Random bot dispatches to a different bot
per race, so its per-opponent deviations average out and its true style variance
is near zero — median $+0.04$ against $+1.02$ for single-race bots on competition
36. Pooling hands it the ladder-wide value regardless, overstating how much style
it has to correlate with, which attenuates every comparison involving one.
Fixing this properly needs a hierarchical estimate that can pull an individual
bot far from the pool when its own data warrants; a hand-set shrinkage weight
does not do it.

**Resolved: the noise model no longer overstates $u$.** Earlier versions of this
model estimated $\hat\sigma^2 = 0.36$ while retrieval was best near $1.0$ — a
factor of nearly three, which suggested $u = 1/\tilde{w} + 1/\tilde{l}$ was
inflating per-cell noise. The cause was single-game cells, whose $u$ reaches
$2.67$, the largest value the formula can produce. Excluding them (§1) moved the
estimate to $1.11$ against a retrieval optimum of about $1.0$, and cut the
fraction of negative per-bot estimates from 36% to 12%.

**One caveat on that correction.** Holding the pair set fixed and varying only
$\hat\sigma^2$, the confirmed `WaterLeak`/`12PoolBot` rewrite sits at the 1.5th
percentile under the old contaminated $0.36$ and the 5.7th under the corrected
$1.11$, while the suspected `who`/`WoundMaker` case moves the other way, 3.9% to
1.1%. The old value was wrong but happened to flatter one labelled case. With
three labelled positives there is no basis for preferring it, and the corrected
parameter is the one in use.

---

## 5. Errors-in-variables model

**What.** Treat each observed residual as a noisy measurement of a latent style
deviation, and let the two bots' latent styles be correlated:

$$
x_{ao} = s_{ao} + \varepsilon_{ao}, \qquad \varepsilon_{ao} \sim \mathcal{N}(0,\, u_{ao})
$$

$$
x_{bo} = s_{bo} + \varepsilon_{bo}, \qquad \varepsilon_{bo} \sim \mathcal{N}(0,\, u_{bo})
$$

$$
\begin{pmatrix} s_{ao} \\ s_{bo} \end{pmatrix} \sim
\mathcal{N}\!\left(\mathbf{0},\;
\begin{pmatrix} \sigma^2 & \rho\sigma^2 \\ \rho\sigma^2 & \sigma^2 \end{pmatrix}\right)
$$

Measurement noise is independent between the two bots, so it lands on the
diagonal only. Marginalising it out is closed form:

$$
\begin{pmatrix} x_{ao} \\ x_{bo} \end{pmatrix} \sim \mathcal{N}\!\left(\mathbf{0},\, \Sigma_o(\rho)\right),
\qquad
\Sigma_o(\rho) = \begin{pmatrix}
\sigma^2 + u_{ao} & \rho\sigma^2 \\
\rho\sigma^2 & \sigma^2 + u_{bo}
\end{pmatrix}
$$

**Why.** $\rho$ is the correlation of the **true** styles, not of the noisy
measurements — the noise inflates the diagonal but never the off-diagonal, so
attenuation from small samples is corrected rather than absorbed into the answer.
This is what lets a thin cell contribute honestly instead of being either
discarded or over-trusted.

---

## 6. Likelihood over rho

$$
\ell(\rho) = \sum_{o \in \mathcal{O}_{ab}}
\left[ -\log 2\pi - \tfrac{1}{2}\log\lvert\Sigma_o(\rho)\rvert
- \tfrac{1}{2}\, z_o^{\top} \Sigma_o(\rho)^{-1} z_o \right],
\qquad z_o = \begin{pmatrix} x_{ao} \\ x_{bo}\end{pmatrix}
$$

Written out for implementation, with $p_o = \sigma^2 + u_{ao}$,
$q_o = \sigma^2 + u_{bo}$ and $c = \rho\,\sigma^2$:

$$
\lvert\Sigma_o\rvert = p_o q_o - c^2
$$

$$
z_o^{\top}\Sigma_o^{-1}z_o =
\frac{q_o\,x_{ao}^2 \;-\; 2c\,x_{ao}x_{bo} \;+\; p_o\,x_{bo}^2}{p_o q_o - c^2}
$$

**How to read the summand.** The cross-term $x_{ao}x_{bo}$ carries the evidence,
and it is a **product** — so an opponent where both bots deviate by 2.7
contributes roughly 28× more than one where both deviate by 0.5. A single large
shared surprise outweighs dozens of small agreements. On the confirmed
`WaterLeak`/`12PoolBot` copy, the top 8 of 78 opponents carry 62% of the signal.

Note the sign symmetry: both bots being *crushed* by the same opponent contributes
exactly as much as both beating it. In practice shared losses dominate — a shared
hard counter is a sharper fingerprint than a shared win, because beating weak bots
is not distinctive. Disagreements contribute negatively and just as heavily.

The division by $\lvert\Sigma_o\rvert$ is what damps imprecise cells, so a big
shared surprise built on 4 games cannot dominate one built on 56.

---

## 7. Posterior and the three outputs

Uniform prior on $\rho \in (-1, 1)$, evaluated on a uniform grid of 79 points
$\rho_k$ from $-0.98$ to $0.98$. Subtracting the maximum is numerical stability
only, and the constant grid spacing cancels between numerator and denominator:

$$
W_k = \exp\!\left(\ell(\rho_k) - \max_j \ell(\rho_j)\right)
$$

$$
\hat\rho = \frac{\sum_k \rho_k W_k}{\sum_k W_k}
$$

$$
\hat\sigma_\rho = \sqrt{\frac{\sum_k (\rho_k - \hat\rho)^2 W_k}{\sum_k W_k}}
$$

$$
P(\rho > \tau) = \frac{\sum_{k \,:\, \rho_k > \tau} W_k}{\sum_k W_k}
$$

with $\tau = 0.5$.

**The three answer different questions, and must not be collapsed.**

- $\hat\rho$ — the **style correlation**: the estimate of the quantity of
  interest. Free of sample-size confounding (correlation with total games
  $+0.07$), and the only one whose magnitude is interpretable on its own. Across
  the labelled cases it tracked how literal the copy was — a clone at $+0.94$, a
  wrapper containing the copied code at $+0.70$, an LLM rewrite at $+0.57$.
- $\hat\sigma_\rho$ — the **posterior spread**: how tightly $\hat\rho$ is pinned
  down. This is the certainty measure. It runs $0.03$ to $0.63$ on real data, is
  nearly a restatement of the shared-opponent count (corr $-0.83$), and carries
  almost nothing about $\rho$ itself (corr $-0.09$).
- $P(\rho > 0.5)$ — posterior mass above a threshold, used to **rank**. It
  requires both a large correlation and enough evidence, which is what stops a
  striking number resting on twelve shared opponents from topping a list.

**$P(\rho > 0.5)$ is not a certainty measure, despite reading like one.** It
fuses *how large the correlation is* with *how well we know it*, correlating
$+0.72$ with $\hat\rho$ itself. Two pairs can share a value near $0.8$ while one
is pinned to $\pm 0.08$ and the other is barely determined at $\pm 0.30$. It
should never be labelled "confidence" unqualified; $\hat\sigma_\rho$ is the
number that answers that question.

**$\hat\sigma_\rho$ is a spread, not half an interval.** The posterior is
markedly left-skewed near the top of the range, because mass piles against the
grid ceiling while the lower tail runs free: the two arms of a 90% interval
differ by a median of $0.21$, and 74% of pairs differ by more than $0.10$. So
$\hat\rho \pm \hat\sigma_\rho$ is not a valid credible interval — on real data it
misplaces an endpoint by a median of $0.12$ and can imply a correlation above
$1$. Quote it as a spread, or take grid quantiles directly.

**On the choice of $\tau = 0.5$.** A choice, not a fact: $0.5$ sits well below
the $0.8+$ where genuinely related bots land. Raising it does not buy
conservatism — past about $0.8$ almost every pair has negligible mass above the
line, so the statistic flattens toward zero and stops discriminating. Read
$\hat\sigma_\rho$ when the threshold's arbitrariness matters.

**Why an effect size and not a Bayes factor.** The tempting formulation — the
posterior probability that the two win rates are *equal* — does not work. It
measures how much evidence exists, not how similar the bots are: with enough
games you can always reject exact equality, since no two distinct programs have
identical win probabilities. Measured, such a Bayes factor correlates $-0.70$
with total games, and it ranked the unrelated, data-poor `12PoolBot`/`Myztery`
pair (421 games) at $+0.57$ "same" while calling the known copy (4926 games)
$-20.94$ "different". The same trap applies to a heterogeneity ($\tau = 0$)
Bayes factor.

---

## 8. Cross-season comparison

Two bots may never have played the same competition. Profiles are then built
per-competition as above — each with its own ELO, its own double-centring and its
own $\hat\sigma^2$ — and compared over opponents that **both faced, in their
respective seasons**.

This works, but drift is large and must be calibrated against. Reference values
from competitions 35 → 36:

| reference | median rho | spread |
|---|---|---|
| **same bot**, c35 profile vs c36 profile (ceiling under drift) | +0.556 | p10 +0.264, p90 +0.784 |
| **different** bots, same race, cross-season (null) | +0.020 | p90 +0.369, p99 +0.665 |

A cross-season $\hat\rho$ of 0.55 therefore means *"as similar as a bot is to
itself a season later"* — which is high, but the null's p99 of 0.665 shows
unrelated pairs do reach that range. Cross-season results are corroborating, not
conclusive.

---

## 9. Scope rules

- **Compare within race**, or where either bot plays Random. Cross-race pairs
  produce coincidental matches; strict same-race filtering was the original rule,
  but it silently discarded a real case — a Zerg bot versus a Random wrapper
  whose Zerg path it had copied — so Random must be exempt.
- A (bot, opponent) cell needs $\ge 2$ games to count at all — see §1.
- A bot needs $\ge 10$ qualifying opponents to be scored.
- A pair needs $\ge 12$ opponents in common.
- Random wrappers cannot be split by the race actually played
  (`match_participation` has no race column), so their profiles are irreducibly
  diluted — see §4.

**Deliberately out of scope: everything except win rates.** Two other signals
were measured and work — step-time fingerprinting (`avg_step_time`) and
game-length signatures — but an LLM asked to rewrite a bot in another language
preserves the strategy while destroying the CPU fingerprint, and catching that
case is the point. Match duration and per-map results are likewise unused.

---

## 10. Validation

Three relationships with external ground truth, plus negative controls:

| comparison | ρ | P(ρ > 0.5) | n |
|---|---|---|---|
| `Mulebot` vs `Snigel` (c35) — **confirmed clone** | +0.938 | 0.999 | 62 |
| `what` vs `who` — **true by construction**, `who` runs `what` on Zerg | +0.696 | 0.913 | 56 |
| `what`(c35) vs `who`(c36) — same, cross-season | +0.730 | 0.947 | 34 |
| `12PoolBot` vs `WaterLeak` (c36) — **confirmed copy**, an LLM rewrite | +0.567 | 0.821 | 78 |
| `12PoolBot` vs `WaterLeak` (c35) — *before* the copy was introduced | -0.039 | 0.015 | 61 |
| `what` vs `QueenBot` — same author, same race, **unrelated** | -0.133 | 0.000 | 65 |
| `WaterLeak` vs `WoundMaker` — same author, same race, **unrelated** | -0.058 | 0.001 | 72 |

The negative controls matter most: two bots by the same author playing the same
race score *negative*, so the model is not simply detecting shared authorship or
shared race. And the `WaterLeak` pair flipping from -0.039 in c35 to +0.567 in
c36 — with `bot_zip_updated` on 2026-04-12, between the two — dates the copy to
that update, which is hard to explain as an artifact.

**The magnitude appears to track how literal the copy is.** `Snigel` is a
confirmed clone and scores +0.938; `WaterLeak` is an LLM rewrite of `12PoolBot`
and scores +0.567. A rewrite preserves the strategy but reimplements it, so it
drifts; a clone does not. That ordering was not designed in and is a useful
sanity check, but it rests on two labelled cases and should not be read as a
calibrated scale from ρ to "degree of copying".

For scale, `Mulebot`/`Snigel` ranks **4th of 1498** scored pairs in competition
35, and 2nd among cross-author pairs — the three above it are one author's own
bots. `Mulebot` is also `Snigel`'s top match out of 28 comparable bots, by a
wide margin (+0.938 against +0.736 for the next), and that runner-up is `clone`,
`Mulebot`'s author's own copy of it.

Figures above predate `MIN_CELL_GAMES`; that change moves ρ by at most 0.01 on
these pairs.

---

## 11. Implementation notes

- **numpy.** The whole model is array work — an $N \times N$ residual matrix, an
  alternating centring loop, and a likelihood vectorised over the 79-point ρ
  grid. Pair scoring is one vectorised call rather than a Python loop over
  opponents.
- **Cost.** Competition 36 is 200 bots and 7205 scored pairs: about 1.1s to
  build, 14ms once cached. Competition 35 is 85 bots and 1449 pairs, 0.3s.
- Compute per competition and cache on a content fingerprint, the same pattern
  as `web/rankings.py`; the sync job warms it after each pass.
- Read the competition scope from `web.season`, never `settings.competition_id`
  — see AGENTS.md.
- This page deliberately does **not** apply `season.ladder_filter()`. A bot
  removed from the ladder mid-season has its division reset to 0 and would
  vanish, and removed bots are precisely the interesting ones.

---

## 12. Reading the output responsibly

$P(\rho > 0.5)$ is the posterior probability that two bots' **style correlation
exceeds 0.5**, conditional on the model. It is **not** the probability that one
copied the other, and must never be reported as such:

- **No base rate.** Nothing in it encodes how often copying happens.
- **Innocent explanations dominate.** A shared public template, the same standard
  build order, or one author's own bots all produce high $\hat\rho$. Two
  independent 12-pool rushers look alike because there is only one way to
  12-pool.
- **Model-conditional.** It assumes the ELO expectation, the double-centring and
  Gaussian style deviations are right enough.

The three outputs answer different questions and should be read together:
$\hat\rho$ is the finding, $P(\rho > 0.5)$ orders candidates, and
$\hat\sigma_\rho$ says how much to believe the finding. A pair can rank well and
still be barely determined.

"Style correlation $0.87$, spread $\pm 0.03$" is a defensible statement.
"99% likely a copy" is not, from the same numbers.
