// SPDX-License-Identifier: BSD-2-Clause
// Verification infrastructure canary only. This is not an NVMe datapath.
`timescale 1ns/1ps
module axis_canary (
    input wire clk, input wire rst,
    input wire [31:0] s_data, input wire s_valid, output wire s_ready,
    output reg [31:0] m_data, output reg m_valid, input wire m_ready
);
    assign s_ready = !rst && (!m_valid || m_ready);
    always @(posedge clk) begin
        if (rst) begin
            m_valid <= 1'b0;
            m_data <= 32'b0;
        end else if (s_ready) begin
            m_valid <= s_valid;
            if (s_valid) m_data <= s_data;
        end
    end
endmodule
