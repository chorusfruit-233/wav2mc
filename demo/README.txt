Demo parameters:
- frequency range: 400-900 Hz
- frequency step: 20 Hz
- phases: 4
- quality: low
- Minecraft Java 26.3 defaults
- resource pack format: 97.1
- data pack format: 121.0
- audio bank revision: 2 (chirps, boundary envelopes, delayed transients)

Files:
- wav2mc_test_bank_400_900hz_4phase.zip: small reusable resource pack
- test_song_datapack.zip: matching data pack
- test_song_preview.wav: local reconstruction preview
- test_song_analysis.json: conversion report
- test_tone.wav: original generated input

Minecraft test:
1. Enable the demo resource pack.
2. Put test_song_datapack.zip in the world's datapacks folder.
3. Run /reload.
4. Run /function test_song:start as a player.
5. Run /function test_song:stop to stop.

Regenerate the intentional demo fixtures from the repository root:
wav2mc bank-build --output demo/wav2mc_test_bank_400_900hz_4phase.zip --min-frequency 400 --max-frequency 900 --frequency-step 20 --phases 4
wav2mc convert demo/test_tone.wav --name test_song --output-dir demo --quality low --min-frequency 400 --max-frequency 900 --frequency-step 20 --phases 4 --preview-resource-pack demo/wav2mc_test_bank_400_900hz_4phase.zip
