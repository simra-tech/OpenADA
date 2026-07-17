require 'digest'
require 'fileutils'
require 'json'

def required_global(name, value)
  raise "missing -rd #{name}=..." if value.nil? || value.empty?
  value
end

layout_path = File.expand_path(required_global('layout_path', $layout_path))
cell_name = required_global('cell_name', $cell_name)
output_path = File.expand_path(required_global('output_path', $output_path))
raise "layout does not exist: #{layout_path}" unless File.file?(layout_path)
raise "output already exists: #{output_path}" if File.exist?(output_path)

width = ($width || '1800').to_i
height = ($height || '1200').to_i
raise 'width and height must be between 64 and 8192' unless
  width.between?(64, 8192) && height.between?(64, 8192)

app = RBA::Application.instance
window = app.main_window
raise 'KLayout GUI context unavailable; run with -z, not -b' unless window
window.load_layout(layout_path, 0)
view = window.current_view
layout = view.active_cellview.layout
cell = layout.cell(cell_name)
raise "cell not found: #{cell_name}" unless cell
view.select_cell(cell.cell_index, 0)
view.add_missing_layers

selected_layers = nil
unless $layers.nil? || $layers.empty?
  selected_layers = $layers.split(',').map do |token|
    match = token.strip.match(/\A(\d+)\/(\d+)\z/)
    raise "invalid layer '#{token}'; expected LAYER/DATATYPE" unless match
    [match[1].to_i, match[2].to_i]
  end.uniq
  available = layout.layer_indices.map do |index|
    info = layout.get_info(index)
    [info.layer, info.datatype]
  end
  missing = selected_layers - available
  raise "layer(s) absent from layout: #{missing.map { |x| x.join('/') }.join(',')}" unless missing.empty?
  view.each_layer do |properties|
    properties.visible = selected_layers.include?(
      [properties.source_layer, properties.source_datatype]
    )
  end
end

if $box.nil? || $box.empty?
  bbox = cell.bbox
  raise "cell has no geometry: #{cell_name}" if bbox.empty?
  dbu = layout.dbu
  x1 = bbox.left * dbu
  y1 = bbox.bottom * dbu
  x2 = bbox.right * dbu
  y2 = bbox.top * dbu
  margin = [x2 - x1, y2 - y1].max * 0.04
  target = RBA::DBox::new(x1 - margin, y1 - margin, x2 + margin, y2 + margin)
else
  values = $box.split(',').map { |value| Float(value.strip) }
  raise 'box must be X1,Y1,X2,Y2 in micrometers' unless values.length == 4
  raise 'box must have positive width and height' unless
    values[2] > values[0] && values[3] > values[1]
  target = RBA::DBox::new(*values)
end

FileUtils.mkdir_p(File.dirname(output_path))
view.save_image_with_options(output_path, width, height, 1, 2, 0, target)
raise "KLayout did not create output: #{output_path}" unless File.file?(output_path)

puts JSON.generate(
  schema: 'openada.layout-visual-review/v1',
  layout: layout_path,
  layout_sha256: Digest::SHA256.file(layout_path).hexdigest,
  cell: cell_name,
  layers: selected_layers&.map { |layer| layer.join('/') },
  box_um: [target.left, target.bottom, target.right, target.top],
  output: output_path,
  output_sha256: Digest::SHA256.file(output_path).hexdigest,
  width_px: width,
  height_px: height
)
