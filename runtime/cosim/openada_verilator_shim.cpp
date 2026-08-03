/* OpenADA's reviewed XSPICE d_cosim <-> Verilator shim.
 *
 * WHY OPENADA OWNS THIS FILE
 * --------------------------
 * ngspice ships its own shim at <prefix>/share/ngspice/scripts/src/
 * verilator_shim.cpp, and OpenADA used it until 2026-08-03. That file has a
 * lifetime defect that makes it unusable for a provenance-bearing tool:
 *
 *   extern "C" void Cosim_setup(struct co_info *pinfo) {
 *       const std::unique_ptr<VerilatedContext> contextp{new VerilatedContext};
 *       Vlng *topp{new Vlng{contextp.get()}};   // keeps a RAW pointer
 *       pinfo->handle = topp;
 *   }                                            // <-- context destroyed HERE
 *
 * The Verilated model outlives the context it was handed, so every later
 * step() dereferences freed storage. With a single instance the freed block
 * usually is not reused and the simulation happens to produce correct
 * results; with two instances the second context reuses the first's memory
 * and ngspice segfaults (reproduced on ngspice-45.2 and ngspice-46).
 *
 * Two further pieces of state are file-scope in the shipped shim and are
 * therefore SHARED by every instance in the process, which corrupts results
 * whenever more than one co-simulated block exists:
 *   - ``static unsigned char previous_output[]`` (the edge-detection memory);
 *   - ``static Digital_t oval`` inside step().
 *
 * The shipped shim also never sets ``pinfo->cleanup``, so the model and its
 * context leak for the lifetime of the process, and its inout input path
 * contains ``topp->name | (1 << ...)`` where ``|=`` was meant -- a statement
 * with no effect.
 *
 * WHAT THIS FILE CHANGES
 * ----------------------
 * Every piece of mutable state becomes PER INSTANCE, held in one heap
 * ``instance`` struct that ``pinfo->handle`` points at and that
 * ``pinfo->cleanup`` releases:
 *   - the VerilatedContext is owned for the model's whole life (no UAF);
 *   - previous_output[] is a member, not a file-scope static;
 *   - oval is an ordinary local.
 * The port-table macros and the co_info protocol are otherwise UNCHANGED from
 * upstream, deliberately: the generated inputs.h/outputs.h/inouts.h tables
 * and ngspice's ABI are the parts that must keep matching the simulator, so
 * this file re-declares ``topp`` and ``previous_output`` as locals that alias
 * the per-instance members and reuses the upstream macro bodies verbatim.
 *
 * The ABI headers (ngspice/cmtypes.h, ngspice/cosim.h) still come from the
 * trusted ngspice installation: ngspice owns the interface, OpenADA owns only
 * the logic on our side of it. This file is digest-bound and re-verified like
 * every other reviewed source in the block library.
 */

// Verilated -*- C++ -*-

#include "verilated.h"
#include "Vlng.h"

#include "ngspice/cmtypes.h" // For Digital_t
#include "ngspice/cosim.h"   // For struct co_info and prototypes

#include <cmath>
#include <cstdlib>
#include <new>

/* This VL_DATA macro is used in the generated header files inputs.h,
 * outputs.h and inouts.h to write accept_input() and step(). The macro is
 * defined several times with different bodies, exactly as upstream does.
 */

/* Port counts, from the generated tables. These are compile-time constants of
 * the model this object was built for, so they are legitimately file-scope.
 */

#define VL_DATA(size, name, msb, lsb) + msb - lsb + 1
static const unsigned int outs = 0
#include "outputs.h"
  ;
static const unsigned int inouts = 0
#include "inouts.h"
  ;
#undef VL_DATA

/* A zero-length array is not valid C++; the port tables always describe at
 * least one output, but keep the floor explicit rather than assumed.
 */
static const unsigned int previous_output_size =
    (outs + inouts) > 0 ? (outs + inouts) : 1;

/* ALL per-instance state. One of these exists per d_cosim device, so two
 * instances in one deck share nothing.
 */

namespace {

struct instance {
    VerilatedContext *contextp;      // owned for the model's whole life
    Vlng             *topp;          // owned
    unsigned char     previous_output[previous_output_size];
};

}  // namespace

/* The input function: it should ignore out-of-range values of index. */

#define VL_DATA(size, name, msb, lsb)      \
  if (index >= msb - lsb + 1) {            \
    index -= msb - lsb + 1;                \
  } else if (msb == 0 && lsb == 0) {       \
    topp->name = val ? 1 : 0;              \
    return;                                \
  } else {                                 \
    if (val)                               \
      topp->name |= (1 << (msb - index));  \
    else                                   \
      topp->name &= ~(1 << (msb - index)); \
    return;                                \
  }

static void accept_input(struct co_info *pinfo,
                         unsigned int index, Digital_t *vp)
{
    instance      *inst = (instance *)pinfo->handle;
    Vlng          *topp = inst->topp;
    unsigned char *previous_output = inst->previous_output;
    unsigned int   val, offset;

    val = vp->state;
    if (val == UNKNOWN)
        return;    // Verilator simulations are two-state.

#include "inputs.h"

    /* For inout ports the new value must be stored to detect changes. */

    offset = outs;
#undef VL_DATA
#define VL_DATA(size, name, msb, lsb)       \
  if (index >= msb - lsb + 1) {             \
    index -= msb - lsb + 1;                 \
    offset += msb - lsb + 1;                \
  } else if (msb == 0 && lsb == 0) {        \
    topp->name = val ? 1 : 0;               \
    previous_output[index + offset] = val;  \
    return;                                 \
  } else {                                  \
    if (val)                                \
      topp->name |= (1 << (msb - index));   \
    else                                    \
      topp->name &= ~(1 << (msb - index));  \
    previous_output[index + offset] = val;  \
    return;                                 \
  }

#include "inouts.h"
    (void)previous_output;
    (void)offset;
}
#undef VL_DATA

/* The step function that calls the Verilator code. */

#ifndef WITH_TIMING
/* The simple case is when the Verilog source contained no time delays,
 * or was compiled with --no-timing.
 */

#define VL_DATA(size, name, msb, lsb)        \
  for (i = msb; i >= lsb; --i) {             \
    if (topp->name & (1 << i))               \
      bit = 1;                               \
    else                                     \
      bit = 0;                               \
    if (bit ^ previous_output[index]) {      \
      previous_output[index] = bit;          \
      oval.state = (Digital_State_t)bit;     \
      (*pinfo->out_fn)(pinfo, index, &oval); \
    }                                        \
    ++index;                                 \
  }

static void step(struct co_info *pinfo)
{
    /* oval is a plain local: upstream made it ``static``, which shares one
     * object across every instance in the process.
     */
    Digital_t      oval = {ZERO, STRONG};
    instance      *inst = (instance *)pinfo->handle;
    Vlng          *topp = inst->topp;
    unsigned char *previous_output = inst->previous_output;
    int            index, i;
    unsigned char  bit;

    topp->eval();

    /* Now scan the outputs for changes. */

    index = 0;
#include "outputs.h"
#include "inouts.h"
}

#else /* WITH_TIMING */

#define VL_DATA(size, name, msb, lsb)        \
  for (i = msb; i >= lsb; --i) {             \
    if (topp->name & (1 << i))               \
      bit = 1;                               \
    else                                     \
      bit = 0;                               \
    if (bit ^ previous_output[index]) {      \
      stop = 1;                              \
      previous_output[index] = bit;          \
      oval.state = (Digital_State_t)bit;     \
      (*pinfo->out_fn)(pinfo, index, &oval); \
    }                                        \
    ++index;                                 \
  }

static void step(struct co_info *pinfo)
{
    Digital_t         oval = {ZERO, STRONG};
    instance         *inst = (instance *)pinfo->handle;
    VerilatedContext *contextp;
    Vlng             *topp = inst->topp;
    unsigned char    *previous_output = inst->previous_output;
    double            tick;
    uint64_t          target, next;
    int               index, i, stop = 0;
    unsigned char     bit;

    /* When the Verilog source was compiled with --timing, run queued events
     * until the Verilog simulation catches up with SPICE.
     */

    contextp = inst->contextp;
    tick = pow(10, contextp->timeprecision());
    target = (uint64_t)(pinfo->vtime / tick);

    /* Step the Verilog simulation towards the target time. */

    do {
      if (topp->eventsPending())
        next = topp->nextTimeSlot();
      else
        next = target;
      if (next >= target) {
        stop = 1;
        next = target;
      }
      contextp->time(next);
      topp->eval();

      /* Scan for output and stop early if found. */

      index = 0;
#include "outputs.h"
#include "inouts.h"
    } while (!stop);

    /* Update the shared simulation time on early exit. */

    if (next < target)
      pinfo->vtime = next * tick;
}

#endif /* WITH_TIMING */
#undef VL_DATA

/* Release this instance. Upstream never set pinfo->cleanup, so the model and
 * its context lived until the process exited.
 */

static void cleanup(struct co_info *pinfo)
{
    instance *inst = (instance *)pinfo->handle;

    if (inst == nullptr)
        return;
    delete inst->topp;
    delete inst->contextp;
    pinfo->handle = nullptr;
    delete inst;
}

extern "C" void Cosim_setup(struct co_info *pinfo)
{
    unsigned int i;

    // Setup context and defaults.

    Verilated::debug(0);

    instance *inst = new (std::nothrow) instance;
    if (inst == nullptr)
        return;                      // ngspice reports the unusable instance.
    for (i = 0; i < previous_output_size; ++i)
        inst->previous_output[i] = 0;

    /* The context is owned by the instance and outlives Cosim_setup, so the
     * Verilated model's pointer to it stays valid for the whole run. This is
     * the defect this file exists to fix.
     */

    inst->contextp = new (std::nothrow) VerilatedContext;
    if (inst->contextp == nullptr) {
        delete inst;
        return;
    }
    inst->topp = new (std::nothrow) Vlng{inst->contextp};
    if (inst->topp == nullptr) {
        delete inst->contextp;
        delete inst;
        return;
    }

    /* Return information to caller. */

    pinfo->handle = inst;
    pinfo->step = step;
    pinfo->cleanup = cleanup;

#define VL_DATA(size, name, msb, lsb) i += msb - lsb + 1; // Count ports

    i = 0;
#include "inputs.h"
    pinfo->in_count = i;

    pinfo->out_count = outs;
    pinfo->inout_count = inouts;
    pinfo->in_fn = accept_input;
#ifdef WITH_TIMING
    pinfo->method = Both;         // There may be immediate results from input.
#else
    pinfo->method = After_input;  // Verilator requires input to advance.
#endif
}
#undef VL_DATA
