# Upstream ngspice report — three defects in the `d_cosim` co-simulation path

**Reported by:** OpenADA (behavioral-block mixed-signal backend), 2026-08-03.
**Affected versions verified:** ngspice **45.2** (Debian `ngspice` package) and
ngspice **46** (built from `ngspice-46` sources, IIC-OSIC-Tools image). Both
reproduce every defect below.
**Severity:** one use-after-free, one segfault from an uninitialized pointer,
and one class of **silently wrong simulation results**.

These are independent defects. (1) is in the shipped Verilator shim; (2) and
(3) are in ngspice core and the `d_cosim` code model, and are NOT fixable by
replacing the shim.

---

## (1) `verilator_shim.cpp` destroys the `VerilatedContext` the model still uses

**File:** `src/xspice/icm/digital/d_cosim/../../../../../share/ngspice/scripts/src/verilator_shim.cpp`
(installed as `<prefix>/share/ngspice/scripts/src/verilator_shim.cpp`).

```c
extern "C" void Cosim_setup(struct co_info *pinfo)
{
    Verilated::debug(0);
    const std::unique_ptr<VerilatedContext> contextp{new VerilatedContext};

    Vlng *topp{new Vlng{contextp.get()}};      // keeps a RAW pointer

    pinfo->handle = topp;
    pinfo->step = step;
    ...
}                                              // <-- contextp destroyed HERE
```

`contextp` is a function-local `unique_ptr`, so the `VerilatedContext` is
destroyed when `Cosim_setup` returns, while the Verilated model keeps the raw
pointer it was constructed with. Every subsequent `step()` therefore operates
on a model whose context has been freed; the `WITH_TIMING` variant
additionally dereferences it directly:

```c
    contextp = topp->contextp();               // dangling
    tick = pow(10, contextp->timeprecision());
```

With a single instance the freed block is typically not reused, so simulations
appear to work — which is how this has gone unnoticed. It is nonetheless
undefined behaviour on every run.

**Two further problems in the same file**, which corrupt results whenever more
than one co-simulated instance exists in a circuit:

* `static unsigned char previous_output[outs + inouts];` is file-scope, so all
  instances share one edge-detection memory;
* `static Digital_t oval` inside `step()` is likewise shared.

**And two smaller ones:**

* `pinfo->cleanup` is never set, so the model and its context leak for the
  lifetime of the process even though `struct co_info` provides the hook;
* in the inout branch of `accept_input()`, `topp->name | (1 << (msb - index));`
  is a statement with no effect — `|=` was clearly intended.

**Fix:** own the context for the model's lifetime and make all mutable state
per-instance. OpenADA now compiles its own corrected shim; it is available at
`runtime/cosim/openada_verilator_shim.cpp` in the OpenADA repository and is
offered upstream under the same terms as the original. The essential change:

```c
struct instance {
    VerilatedContext *contextp;                 // owned, outlives Cosim_setup
    Vlng             *topp;
    unsigned char     previous_output[previous_output_size];
};
/* pinfo->handle = inst;  pinfo->cleanup = cleanup;  (deletes both) */
```

---

## (2) `cm_irreversible()` leaves an uninitialized slot in `evt->info.hybrids`

**File:** `src/xspice/cm/cm.c`, function `cm_irreversible()`.

In the "instance is not hybrid, add an entry" branch:

```c
        for (i = num_hybrids - 2; i >= 0; --i) {
            value = hybrids[i]->irreversible;
            if (value != 0 && value < place) {
                hybrids[i + 1] = hybrids[i];
            } else if (value == place) {
                duplicate(instance);            /* warns, then FALLS THROUGH */
            } else {
                break;
            }
        }
        hybrids[i + 1] = instance;
```

When an equal `place` is found, `duplicate()` only prints a warning and the
loop **continues** instead of stopping. (The sibling "existing hybrid" branch
above does `duplicate(instance); break;` — the `break` is missing here.)

With two instances both requesting `place == 1`:

* the loop runs from `i = 0`, hits `value == place`, warns, decrements to
  `i == -1` and exits;
* `hybrids[i + 1]` is `hybrids[0]`, so the second instance **overwrites** the
  first;
* `num_hybrids` is now 2, but `hybrids[1]` is whatever `TREALLOC` returned —
  uninitialized memory.

`EVTcall_hybrids()` (`src/xspice/evt/evtcall_hybrids.c`) then iterates
`hybrids[0 .. num_hybrids-1]` and passes each to `EVTload_with_event()`,
dereferencing the uninitialized pointer:

```
Warning: Duplicate value 1 in cm_irreversible() for instance a2.
Segmentation fault
```

**Fix:** add the missing `break;` after `duplicate(instance);`, matching the
other branch. With it, `i` stays 0 and the instance is correctly placed at
`hybrids[1]`.

### Minimal reproducer (no Verilator needed)

`mini.c` — a complete `d_cosim` shim with fully per-instance state, so the
crash cannot be blamed on the shipped shim's shared statics:

```c
#include <stdlib.h>
#include "ngspice/cmtypes.h"
#include "ngspice/cosim.h"

struct inst { unsigned char last_out; unsigned char in0; };

static void accept_input(struct co_info *pinfo, unsigned int index, Digital_t *vp)
{
    struct inst *ip = (struct inst *)pinfo->handle;
    if (index != 0 || vp->state == UNKNOWN) return;
    ip->in0 = vp->state ? 1 : 0;
}

static void step(struct co_info *pinfo)
{
    struct inst *ip = (struct inst *)pinfo->handle;
    Digital_t oval = {ZERO, STRONG};
    unsigned char bit = ip->in0 ? 0 : 1;
    if (bit != ip->last_out) {
        ip->last_out = bit;
        oval.state = (Digital_State_t)bit;
        (*pinfo->out_fn)(pinfo, 0, &oval);
    }
}

static void cleanup(struct co_info *pinfo) { free(pinfo->handle); pinfo->handle = NULL; }

void Cosim_setup(struct co_info *pinfo)
{
    struct inst *ip = calloc(1, sizeof *ip);
    pinfo->handle = ip;  pinfo->step = step;  pinfo->cleanup = cleanup;
    pinfo->in_count = 1; pinfo->out_count = 1; pinfo->inout_count = 0;
    pinfo->in_fn = accept_input; pinfo->method = After_input;
}
```

Build: `gcc -fpic -shared -I<ngspice scripts/src> -o mini.so mini.c`

`two.sp` — two instances of the **same** model:

```
* TWO d_cosim instances
Vc c 0 PULSE(0 1 10n 1n 1n 20n 40n)
Vd d 0 PULSE(0 1 15n 1n 1n 20n 40n)
Abr  [c] [dc] abr
Abr2 [d] [dd] abr
.model abr adc_bridge(in_low=0.4 in_high=0.6)
A1 [dc] [q1] mm
A2 [dd] [q2] mm
.model mm d_cosim(simulation="./mini.so")
Ad [q1 q2] [o1 o2] dac
.model dac dac_bridge(out_low=0 out_high=1)
.tran 0.5n 80n
.control
run
.endc
.end
```

`ngspice -b two.sp` → `Warning: Duplicate value 1 ...` then **SIGSEGV**, on
both 45.2 and 46. One instance runs correctly. Setting `irreversible=0`, or
giving the two instances *different* models with distinct `irreversible`
values, avoids the crash — which confirms the duplicate-`place` path as the
cause.

Note that `irreversible` is a **model** parameter, so two instances of one
`.model` card cannot be given distinct places at all; the crash is
unavoidable for that (very natural) netlist.

---

## (3) Two co-existing `d_cosim` instances silently produce wrong results

Even when the crash in (2) is avoided by giving each instance a distinct
`irreversible` place, **the co-simulation results depend on the order in which
the instances appear in the netlist**, with no warning or diagnostic.

Reproduced with two *different* co-simulated blocks (a clocked comparator and
a synchronous buck switch pair) in one deck, using OpenADA's corrected shim so
that (1) is excluded:

| Netlist order                    | buck mean V(out) | expected |
|----------------------------------|------------------|----------|
| buck instantiated first          | **1.885890 V**   | 1.885883 V (standalone) |
| comparator instantiated first    | **1.250168e-07 V** | 1.885883 V |

Both results are perfectly reproducible across runs. The buck converter's
output collapses to ~0 V purely because another `d_cosim` instance was
instantiated ahead of it. Swapping the two `irreversible` places does not
change the outcome, so it is the instantiation/hybrid load order that matters,
not the requested place.

We have not root-caused this one; it is presumably related to how an
irreversible (non-backtrackable) co-simulator interacts with a rejected
timestep when more than one such instance exists — `EVTcall_hybrids()` breaks
out of its loop when `g_mif_info.breakpoint.current < ckt->CKTtime`, which
leaves the remaining hybrids un-loaded for that step, and a co-simulator that
has already advanced cannot be rewound.

**Impact:** this is the most serious of the three for any tool that trusts
ngspice's output, because there is no crash and no diagnostic — just a wrong
number. Because of it, OpenADA refuses multi-instance `d_cosim` decks outright
rather than emitting a deck whose correctness depends on card order.

---

## Summary of requested fixes

1. `verilator_shim.cpp`: own the `VerilatedContext` per instance, move
   `previous_output[]` and `oval` into per-instance state, set
   `pinfo->cleanup`, and fix the `|` → `|=` typo. (Patch available.)
2. `cm.c` / `cm_irreversible()`: add the missing `break;` after
   `duplicate(instance);` in the add-an-entry branch.
3. Investigate multi-instance `d_cosim` correctness, or document and enforce a
   one-instance-per-circuit limit — silently order-dependent results are worse
   than a refusal.
