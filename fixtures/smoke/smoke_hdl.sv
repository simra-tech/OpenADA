module smoke_counter(input logic clk, input logic rst_n, output logic [1:0] q);
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) q <= 2'b00;
    else q <= q + 2'b01;
  end
endmodule

module tb;
  logic clk = 0;
  logic rst_n = 0;
  logic [1:0] q;

  smoke_counter dut(.clk(clk), .rst_n(rst_n), .q(q));

  always #1 clk = ~clk;

  initial begin
    #2 rst_n = 1;
    #8;
    $display("SMOKE q=%0d", q);
    if (q !== 2'd0) $fatal(1, "unexpected counter value");
    $finish;
  end
endmodule
