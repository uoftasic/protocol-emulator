/*
 * Copyright (c) 2024 Your Name
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// The UART controller lives in L1-PEm-system/L2-uart-controller/uart-controller.sv.

// Tiny Tapeout pin adapter:
// ui_in      = transmit data
// uio_in[0]  = UART RX, uio_in[1] = transmit enable
// uo_out     = received data
// uio_out[0] = UART TX
module tt_um_uoftasic_protocol_emulator (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  wire [7:0] rx_data;

  uart_controller #(
      .CLK_PER_BIT(434),
      .DATA_WIDTH(8),
      .NUM_DATA_BITS(8),
      .NUM_STOP_BITS(1),
      .PARITY_EN(0)
  ) uart (
      .clk       (clk),
      .rst       (1'b0),
      .ares      (!rst_n),
      .i_uart_rx (uio_in[0]),
      .o_uart_tx (uio_out[0]),
      .i_tx_data (ui_in),
      .i_tx_en   (uio_in[1] && ena),
      .o_rx_data (rx_data),
      .o_rx_ready()
  );

  assign uo_out  = rx_data;
  assign uio_out[7:1] = 7'b0;
  assign uio_oe  = 8'b0000_0001;
endmodule

`default_nettype wire
