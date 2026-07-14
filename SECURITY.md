# Security policy

OpenADA launches local EDA executables against user-supplied design files. Treat
an EDA binary, PDK hook, rule deck, Tcl/Python script, netlist control block, and
project Makefile as executable or potentially executable input.

## Supported versions

Only the latest tagged preview receives security fixes while the project is in
the `0.x` series.

## Reporting a vulnerability

Do not publish an exploit or sensitive design data in a public issue. Use
[GitHub's private security advisory flow](https://github.com/simra-tech/OpenADA/security/advisories/new)
for command injection, path escape, unsafe file replacement, unbounded output,
or malicious-input findings.

Include the affected OpenADA version, operation, platform, minimal reproduction,
and whether the native EDA actually launched. Redact proprietary design and PDK
content.

## Trust boundary

OpenADA avoids shell invocation and bounds captured output, but it does not
sandbox native EDA tools. Run untrusted designs or collateral inside an
appropriate container or operating-system sandbox and review generated scripts
before execution.
