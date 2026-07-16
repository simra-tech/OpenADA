read_liberty "/design/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib"
read_verilog "/evidence/synthesis/mapped.v"
link_design "ibex_core"
read_sdc "/evidence/timing/timing-input.sdc"
puts OPENADA_UNITS_BEGIN
report_units
puts OPENADA_UNITS_END
check_setup -verbose -unconstrained_endpoints > check-setup.txt
report_checks -path_delay max -group_path_count 10 -endpoint_path_count 1 -format json > setup-paths.json
report_checks -path_delay min -group_path_count 10 -endpoint_path_count 1 -format json > hold-paths.json
puts OPENADA_SETUP_WNS_BEGIN
report_worst_slack -max -digits 9
puts OPENADA_SETUP_WNS_END
puts OPENADA_SETUP_TNS_BEGIN
report_tns -max -digits 9
puts OPENADA_SETUP_TNS_END
puts OPENADA_HOLD_WNS_BEGIN
report_worst_slack -min -digits 9
puts OPENADA_HOLD_WNS_END
puts OPENADA_HOLD_TNS_BEGIN
report_tns -min -digits 9
puts OPENADA_HOLD_TNS_END
puts OPENADA_ANALYSIS_COMPLETE
