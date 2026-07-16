# The Xyce&trade; Regression Test Suite

This is the test suite associated with the [Xyce](https://xyce.sandia.gov)
analog circuit simulator, in this GitHub project (<https://github.com/Xyce>).
The documentation for how to use this test suite is available on the [Xyce
Homepage](https://xyce.sandia.gov) at
<https://xyce.sandia.gov/documentation/RunningTheTests.html>.

The Xyce Regression Suite contains hundreds of tests intended to demonstrate
correct functioning of Xyce on any platform. In addition to basic function
tests, the suite also contains test cases designed to verify that particular
bugs have been fixed, and to ensure they don't get re-introduced. These tests
are run nightly on all our supported platforms, and also by Xyce developers in
the course of their work.

## Attention!!! Open source users
Due to an internal process change, the Xyce team has decided to break/restart the project history.
The master branch has been moved to track changes rooted in a new orphaned node/commit in the repository.
The new root commit is based on the final merge commit for the 7.9 release (so that there are no differences between the top of the old tree and this new root commit).
All future updates will be pushed to the re-rooted master branch.

Users may encounter the following error trying to update their repositories:\
  `fatal: refusing to merge unrelated histories`

To resolve the issue, we recommend deleting your local master branch and creating a new branch to track origin/master.  
  `git fetch`\
  `git checkout -b temp_branch`\
  `git branch -D master`\
  `git checkout master`\
  `git branch -D temp_branch`
  
To preserve a continuous view of the project history; replace the new root commit with the head of the old history.\
  `git replace <new root tag/commit id> <origin/old_master or commit id>`

We apologize for any inconvenience.

## About Xyce

[Xyce](https://xyce.sandia.gov) (z&#x012B;s, rhymes with "spice") is an open
source, SPICE-compatible, high-performance analog circuit simulator, capable of
solving extremely large circuit problems by supporting large-scale parallel
computing platforms. It also supports serial execution on all common desktop
platforms, and small-scale parallel runs on Unix-like systems. In addition to
analog electronic simulation, Xyce has also been used to investigate more
general network systems, such as neural networks and power grids. In providing
an Open Source version of Xyce to the external community, Sandia hopes to
contribute a robust and modern electronic simulator to users and researchers in
the field.

The Xyce repository is available on GitHub
[here](https://github.com/Xyce/Xyce).

## Contributing

We welcome bug reports and enhancement requests. Those can be done through the
GitHub "Issues" area, or [other
methods](https://xyce.sandia.gov/contact_us.html). Due to internal
restrictions, however, it is difficult for us to accept external contributions
at this time, which includes pull requests. Nevertheless, if you would like to
discuss the possibility of a contribution, please contact us.

## Copyright and license

Copyright 2019 National Technology & Engineering Solutions of Sandia, LLC
(NTESS). Under the terms of Contract DE-NA0003525 with NTESS, the U.S.
Government retains certain rights in this software.

Xyce&trade; is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

Xyce&trade; is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

A copy of the GNU General Public License is included in the
[COPYING](./COPYING) file, or see <http://www.gnu.org/licenses/>.

## Acknowledgements

Xyce has been funded by the NNSA's Advanced Simulation and Computing (ASC)
Campaign, the DARPA POSH program, and the Laboratory Directed Research and
Development program at Sandia National Laboratories. Sandia National
Laboratories is a multimission laboratory managed and operated by National
Technology & Engineering Solutions of Sandia, LLC, a wholly owned subsidiary of
Honeywell International Inc., for the U.S. Department of Energy's National
Nuclear Security Administration under contract DE-NA0003525.

SAND2019-5200 O

