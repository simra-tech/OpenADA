# Generate a tiny GDS for the KLayout DRC smoke.
raise "pass -rd output=/absolute/path/to/toy.gds" unless $output

layout = RBA::Layout::new
layout.dbu = 0.001
cell = layout.create_cell("TOP")
layer = layout.layer(1, 0)
cell.shapes(layer).insert(RBA::Box::new(0, 0, 1000, 2000))
layout.write($output)
