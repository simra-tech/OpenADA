/* Dummy main() for Verilator.
 *
 * Verilator only compiles its generated sources when asked to build an
 * executable, so a main() must exist at link time even though the artifact
 * OpenADA keeps is the shared object, not the program. This is upstream's
 * ngspice scripts/src/verilator_main.cpp, carried here so that the whole
 * translation set OpenADA compiles is reviewed and digest-bound rather than
 * half-owned. It is never executed by ngspice.
 */

#include "ngspice/cmtypes.h" // For Digital_t
#include "ngspice/cosim.h"   // For struct co_info and prototypes

int main(int, char **, char **) {
    struct co_info info = {};

    Cosim_setup(&info);
    for (;;)
        (*info.step)(&info);
    return 0;
}
