# Wiring

> **Safety first.** Everything in this file is **low voltage** (3.3 V / 5 V from the
> ESP32 / USB). During development the relays switch **LEDs only**.
> **Nothing here may be connected to 230 V mains.** Mains wiring is the last step of
> the project and is done only with the department's permission, by or under a
> qualified electrician, using an opto-isolated AC-rated relay module, with the
> logic side and the mains side physically separated (see the end of this file).

## Pin map (ESP32 DevKit, 30/38-pin)

| Part | Part pin | ESP32 pin | Notes |
|------|----------|-----------|-------|
| HC-SR501 PIR | OUT | **GPIO27** | VCC → **VIN (5 V)**, GND → GND. Output is 3.3 V, safe for the ESP32 |
| DHT11 | DATA | **GPIO26** | VCC → 3.3 V, GND → GND. Bare 4-pin sensor: 10 kΩ from DATA to VCC (3-pin modules have it built in) |
| LDR divider | middle point | **GPIO32** (ADC1) | 3.3 V → LDR → GPIO32 → resistor → GND (see below) |
| ACS712 current sensor | OUT via divider | **GPIO34** (ADC1, input only) | **Not used yet** - only after mains wiring |
| ZMPT101B voltage sensor | OUT | **GPIO35** (ADC1, input only) | **Not used yet** - only after mains wiring |
| Relay module | IN1 (light) | **GPIO25** | |
| Relay module | IN2 (fan) | **GPIO33** | |
| Relay module | IN3 / IN4 | **GPIO18 / GPIO19** | Spare, kept OFF by the firmware |
| Relay module | VCC / GND | **VIN (5 V)** / GND | Common GND with the ESP32 is required |

Why these pins:
- **All analog inputs are on ADC1** (GPIO32-39). ADC2 pins cannot be read while Wi-Fi is on.
- GPIO34/35 are input-only, which is fine for sensors.
- None of the pins is a boot "strapping" pin (0, 2, 5, 12, 15), so the board always boots normally.

```
            ESP32
          +--------+
  PIR OUT-| GPIO27 |
DHT DATA -| GPIO26 |
  LDR mid-| GPIO32 |
          |        |
     IN1 -| GPIO25 |   relay 1 = LIGHT
     IN2 -| GPIO33 |   relay 2 = FAN
     IN3 -| GPIO18 |   spare
     IN4 -| GPIO19 |   spare
          |        |
 ACS712 ->| GPIO34 |   (later, via divider)
ZMPT101B->| GPIO35 |   (later)
          |  VIN 5V|-> PIR VCC, relay VCC
          |   3V3  |-> DHT11 VCC, LDR top
          |   GND  |-> all GNDs
          +--------+
```

## Relay outputs: LED test circuit (development)

Each relay has three screw terminals: **COM**, **NO** (normally open) and **NC**.
For testing, wire an LED so it lights when the relay is ON:

```
 5 V (VIN) ---- COM
                NO ---- 330 Ω ---- LED (+ long leg)  LED (- short leg) ---- GND
```

- Use **NO**, not NC: then the LED is off when the relay is off.
- One LED per relay you want to watch. Most relay modules also have a small
  indicator LED per channel on the board.
- Safe low-voltage alternative for a more realistic demo: a 12 V DC fan and a
  12 V LED strip on the relay contacts, powered by a 12 V DC adapter.

### Relay polarity

Most cheap relay boards are **active-low** (relay ON when IN is 0 V).
This is the setting `RELAY_ACTIVE_LOW = True` in `esp32/config.py`.
Test it with the polarity test in the README before relying on it.

## LDR voltage divider

```
 3.3 V ---- LDR ---- GPIO32 ---- R ---- GND
```

Brighter light → lower LDR resistance → higher voltage → higher reading (0-4095).

Choosing **R**: it should be about the LDR's resistance in *normal room light*.
- Reading stays near **0** in room light (only reacts to a torch) → **R is too small**:
  try 10 kΩ → 47 kΩ → 100 kΩ.
- Reading stays near **4095** → R is too large, or GPIO32 is shorted to 3.3 V.

With an **LDR module** (VCC, GND, AO, DO): connect **AO** to GPIO32. DO is only
on/off (0 or 4095). Power the module from **3.3 V**, not 5 V.

The firmware averages 5 ADC readings with 11 dB attenuation (0-3.3 V range).
The LDR measures **daylight** (put it near a window); a small LED will not change it.

## ACS712 voltage divider (for later)

The ACS712 runs on 5 V, so its output can reach **5 V**, but an ESP32 pin must
never see more than **3.3 V**. A divider scales it down:

```
 ACS712 OUT ---- 2.2 kΩ ---- GPIO34 ---- 4.4 kΩ ---- GND
```

- Ratio = 4.4 / (2.2 + 4.4) = **0.667** → 5.0 V becomes **3.33 V**.
- 4.4 kΩ is not a standard value: use **two 2.2 kΩ in series**.
- In code: `sensor_voltage = adc_voltage / 0.667`.
- The ACS712 has no current flowing through it until the mains stage, so it is not used yet.

## ZMPT101B (for later)

The module's output sits around half its supply voltage. Make sure it stays below
3.3 V at GPIO35 (power the module from 3.3 V, or use the same divider as above).
Calibration is done against a multimeter in the mains stage.

## Common problems

| Symptom | Cause |
|---------|-------|
| DHT11 `read failed` every time | loose DATA wire, wrong pin order on a 3-pin module, or missing 10 kΩ pull-up on a bare sensor |
| PIR always 1 | still warming up (wait 60 s), sensitivity pot too high, or it "sees" a moving fan / sunlight |
| PIR stays 1 for long after motion | its time pot is high - turn it fully anticlockwise (minimum); the backend does the hold |
| Relay never clicks | relay VCC not 5 V, no common GND, or IN wire on the wrong GPIO |
| Relay ON when it should be OFF | polarity: change `RELAY_ACTIVE_LOW` |
| Relay stays ON whatever the pin does | some 5 V relay boards need the input to go to 5 V to switch off; remove the JD-VCC jumper and power JD-VCC with 5 V, VCC with 3.3 V |

## Mains (last stage only - not now)

Before anything touches 230 V:
1. Written/explicit permission from the department.
2. Done by or under a **qualified electrician**.
3. **Opto-isolated, AC-rated** relay module with a rating well above the load
   (extra margin for the fan's motor start-up current).
4. Logic side (ESP32, sensors, USB) and mains side **physically separated**;
   no mains on breadboards or jumper wires; mains parts in a closed, insulated
   enclosure with proper terminals and a fuse/MCB.
5. Power off while anything is changed; tests only with the electrician present.

The firmware and backend do not change for mains loads; only `LIGHT_W`, `FAN_W`
and possibly `RELAY_ACTIVE_LOW` in `config.py`.
