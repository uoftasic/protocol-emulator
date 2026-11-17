// -----------------------------------------------------------------------------
// uart-controller.sv
// Parameterized UART controller (TX + RX, single clock domain)
//
// Behaviour summary
//   * Frames are LSB first on an idle-high line:
//       start(0) | NUM_DATA_BITS data | [parity] | NUM_STOP_BITS stop(1)
//   * Parity, when PARITY_EN=1, is even parity.
//   * TX : a one-clock i_tx_en pulse while the transmitter is idle latches
//          i_tx_data and starts a frame. Requests while busy are dropped.
//   * RX : the start bit is re-checked at mid-bit (false starts are ignored),
//          then each bit is sampled at mid-bit. On a valid frame, o_rx_data
//          updates and o_rx_ready pulses for one clock. Frames with a bad stop
//          bit or bad parity are discarded.
//
// Resets
//   * ares : active-high asynchronous reset
//   * rst  : active-high synchronous reset
// -----------------------------------------------------------------------------

`default_nettype none

module uart_controller #(
    parameter int CLK_PER_BIT   = 434,
    parameter int DATA_WIDTH    = 8,
    parameter int NUM_DATA_BITS = DATA_WIDTH,
    parameter int NUM_STOP_BITS = 1,
    parameter int PARITY_EN     = 0
) (
    // General
    input  logic                  clk,
    input  logic                  rst,     // active-high sync reset
    input  logic                  ares,    // active-high async reset

    // Serial lines
    input  logic                  i_uart_rx,
    output logic                  o_uart_tx,

    // TX interface
    input  logic [DATA_WIDTH-1:0] i_tx_data,
    input  logic                  i_tx_en,

    // RX interface
    output logic [DATA_WIDTH-1:0] o_rx_data,
    output logic                  o_rx_ready
);

  // ---------------------------------------------------------------------------
  // Local parameters
  // ---------------------------------------------------------------------------
  localparam int BIT_COUNT_WIDTH    = (CLK_PER_BIT   <= 1) ? 1 : $clog2(CLK_PER_BIT);
  localparam int FRAME_BITS         = 1 + NUM_DATA_BITS + PARITY_EN + NUM_STOP_BITS;
  localparam int FRAME_COUNT_WIDTH  = (FRAME_BITS    <= 2) ? 1 : $clog2(FRAME_BITS);
  localparam int STOP_COUNT_WIDTH   = (NUM_STOP_BITS <= 1) ? 1 : $clog2(NUM_STOP_BITS);
  localparam int RX_BIT_COUNT_WIDTH = (NUM_DATA_BITS <= 1) ? 1 : $clog2(NUM_DATA_BITS);

  initial begin
    if (CLK_PER_BIT < 1 || NUM_DATA_BITS < 1 || NUM_STOP_BITS < 1 ||
        NUM_STOP_BITS > 2 || DATA_WIDTH < NUM_DATA_BITS) begin
      $error("Invalid UART parameters");
    end
  end

  // ---------------------------------------------------------------------------
  // TX
  // ---------------------------------------------------------------------------
  logic [BIT_COUNT_WIDTH-1:0]   tx_clk_count;
  logic [FRAME_COUNT_WIDTH-1:0] tx_frame_position;
  logic [DATA_WIDTH-1:0]        tx_data_reg;
  logic                         tx_parity;
  logic                         tx_active;
  logic                         tx_serial_reg;

  // Line level for a given frame position (0 = start bit).
  // Declared after the TX registers it reads: Icarus rejects use-before-declaration.
  function automatic logic uart_tx_symbol(input int position);
    if (position == 0)
      return 1'b0;
    else if (position <= NUM_DATA_BITS)
      return tx_data_reg[position-1];
    else if (PARITY_EN != 0 && position == NUM_DATA_BITS + 1)
      return tx_parity;
    else
      return 1'b1;
  endfunction

  assign o_uart_tx = tx_serial_reg;

  always_ff @(posedge clk or posedge ares) begin
    if (ares) begin
      tx_clk_count      <= '0;
      tx_frame_position <= '0;
      tx_data_reg       <= '0;
      tx_parity         <= 1'b0;
      tx_active         <= 1'b0;
      tx_serial_reg     <= 1'b1;
    end else if (rst) begin
      tx_clk_count      <= '0;
      tx_frame_position <= '0;
      tx_data_reg       <= '0;
      tx_parity         <= 1'b0;
      tx_active         <= 1'b0;
      tx_serial_reg     <= 1'b1;
    end else if (!tx_active) begin
      tx_clk_count <= '0;
      if (i_tx_en) begin
        tx_data_reg       <= i_tx_data;
        tx_parity         <= ^i_tx_data[NUM_DATA_BITS-1:0];
        tx_frame_position <= '0;
        tx_active         <= 1'b1;
        tx_serial_reg     <= 1'b0;
      end else begin
        tx_serial_reg <= 1'b1;
      end
    end else if (tx_clk_count == CLK_PER_BIT - 1) begin
      tx_clk_count <= '0;
      if (tx_frame_position == FRAME_BITS - 1) begin
        tx_active     <= 1'b0;
        tx_serial_reg <= 1'b1;
      end else begin
        tx_frame_position <= tx_frame_position + 1'b1;
        tx_serial_reg     <= uart_tx_symbol(int'(tx_frame_position) + 1);
      end
    end else begin
      tx_clk_count <= tx_clk_count + 1'b1;
    end
  end

  // ---------------------------------------------------------------------------
  // RX
  // ---------------------------------------------------------------------------
  typedef enum logic [2:0] {
    RX_IDLE   = 3'd0,
    RX_START  = 3'd1,
    RX_DATA   = 3'd2,
    RX_PARITY = 3'd3,
    RX_STOP   = 3'd4
  } rx_state_t;

  rx_state_t                     rx_state;
  logic [BIT_COUNT_WIDTH-1:0]    rx_clk_count;
  logic [RX_BIT_COUNT_WIDTH-1:0] rx_bit_position;
  logic [STOP_COUNT_WIDTH-1:0]   rx_stop_position;
  logic [DATA_WIDTH-1:0]         rx_data_reg;
  logic                          rx_parity;
  logic                          rx_parity_error;

  always_ff @(posedge clk or posedge ares) begin
    if (ares) begin
      rx_state         <= RX_IDLE;
      rx_clk_count     <= '0;
      rx_bit_position  <= '0;
      rx_stop_position <= '0;
      rx_data_reg      <= '0;
      rx_parity        <= 1'b0;
      rx_parity_error  <= 1'b0;
      o_rx_data        <= '0;
      o_rx_ready       <= 1'b0;
    end else if (rst) begin
      rx_state         <= RX_IDLE;
      rx_clk_count     <= '0;
      rx_bit_position  <= '0;
      rx_stop_position <= '0;
      rx_data_reg      <= '0;
      rx_parity        <= 1'b0;
      rx_parity_error  <= 1'b0;
      o_rx_data        <= '0;
      o_rx_ready       <= 1'b0;
    end else begin
      o_rx_ready <= 1'b0;
      case (rx_state)
        RX_IDLE: begin
          rx_clk_count <= '0;
          if (!i_uart_rx) begin
            rx_state        <= RX_START;
            rx_parity_error <= 1'b0;
          end
        end

        RX_START: begin
          if (rx_clk_count == (CLK_PER_BIT / 2) - 1) begin
            rx_clk_count    <= '0;
            if (i_uart_rx) rx_state <= RX_IDLE;   // false start
            else           rx_state <= RX_DATA;
            rx_bit_position <= '0;
            rx_parity       <= 1'b0;
          end else begin
            rx_clk_count <= rx_clk_count + 1'b1;
          end
        end

        RX_DATA: begin
          if (rx_clk_count == CLK_PER_BIT - 1) begin
            rx_clk_count                 <= '0;
            rx_data_reg[rx_bit_position] <= i_uart_rx;
            rx_parity                    <= rx_parity ^ i_uart_rx;
            if (rx_bit_position == NUM_DATA_BITS - 1) begin
              if (PARITY_EN != 0) rx_state <= RX_PARITY;
              else                rx_state <= RX_STOP;
              rx_stop_position <= '0;
            end else begin
              rx_bit_position <= rx_bit_position + 1'b1;
            end
          end else begin
            rx_clk_count <= rx_clk_count + 1'b1;
          end
        end

        RX_PARITY: begin
          if (rx_clk_count == CLK_PER_BIT - 1) begin
            rx_clk_count     <= '0;
            rx_parity_error  <= (i_uart_rx != rx_parity);
            rx_stop_position <= '0;
            rx_state         <= RX_STOP;
          end else begin
            rx_clk_count <= rx_clk_count + 1'b1;
          end
        end

        RX_STOP: begin
          if (rx_clk_count == CLK_PER_BIT - 1) begin
            rx_clk_count <= '0;
            if (rx_stop_position == NUM_STOP_BITS - 1) begin
              if (i_uart_rx && !rx_parity_error) begin
                o_rx_data  <= rx_data_reg;
                o_rx_ready <= 1'b1;
              end
              rx_state <= RX_IDLE;
            end else begin
              rx_stop_position <= rx_stop_position + 1'b1;
            end
          end else begin
            rx_clk_count <= rx_clk_count + 1'b1;
          end
        end

        default: rx_state <= RX_IDLE;
      endcase
    end
  end

endmodule

`default_nettype wire
