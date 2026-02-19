from glasgow.applet import GlasgowAppletV2TestCase, synthesis_test, applet_v2_simulation_test
from . import I2SInputApplet

class I2SInputAppletTestCase(GlasgowAppletV2TestCase, applet=I2SInputApplet):
    @synthesis_test
    def test_build(self):
        self.assertBuilds()

    @applet_v2_simulation_test(args=["--bclk", "A0", "--ws", "A1", "--sd", "A2"])
    async def test_record(self, applet, ctx):
        # print(f"DEBUG: ctx type: {type(ctx)}, dir: {dir(ctx)}")
        # I2S pins from applet assembly
        bclk = applet.assembly.get_pin("A0")
        ws   = applet.assembly.get_pin("A1")
        sd   = applet.assembly.get_pin("A2")

        async def send_i2s(left, right, bit_depth=16):
            # Start with WS=1 (Right) to see a transition to 0 (Left)
            ctx.set(ws.i, 1)
            ctx.set(bclk.i, 0)
            await ctx.tick()
            # Wait for synchronizer
            await ctx.tick()
            await ctx.tick()

            for word, ws_val in [(left, 0), (right, 1)]:
                # WS transition
                ctx.set(ws.i, ws_val)
                # One BCLK cycle wait (I2S standard: MSB is 1 cycle after WS change)
                ctx.set(bclk.i, 1)
                await ctx.tick()
                ctx.set(bclk.i, 0)
                await ctx.tick()

                # Send bits
                for i in range(bit_depth):
                    bit = (word >> (bit_depth - 1 - i)) & 1
                    ctx.set(sd.i, bit)
                    ctx.set(bclk.i, 1)
                    await ctx.tick()
                    ctx.set(bclk.i, 0)
                    await ctx.tick()
            
            # Idle cycles to flush the pipe
            for _ in range(200):
                await ctx.tick()

        # We can't use ctx.fork easily it seems, so let's try to interleave or just run sequentially
        # If the gateware is active, running sequentially should work if we have enough buffer.
        # But wait, send_i2s takes time.
        
        # Actually, let's try to use a background testbench for sending.
        # But we can't add it now.
        
        # Let's just use asyncio.gather and hope it works with the simulator's tick()
        # Wait, the simulator's tick() is what advances time.
        # If two tasks call tick(), they will both advance the simulator?
        # That's not how it usually works.
        
        # Okay, let's just do it sequentially and see if it works.
        # I'll use a larger bit depth to be sure.
        await send_i2s(0x1234, 0x5678)

        # Read back the data from the pipe
        data = await applet.i2s_iface.read(4)
        self.assertEqual(bytes(data), b"\x12\x34\x56\x78")
