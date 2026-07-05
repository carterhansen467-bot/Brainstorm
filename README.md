![Brainstorm-mod logo](Assets/BrainstormLogo.jpg)
--
## Requirements
- [Lovely](https://github.com/ethangreen-dev/lovely-injector) injector -- Get it here: https://github.com/ethangreen-dev/lovely-injector/releases

## Installation

1. Install [Lovely](https://github.com/ethangreen-dev/lovely-injector) and follow the manual installation instructions.

### Windows

2. Download the [latest release](https://github.com/OceanRamen/Brainstorm/releases/) of Brainstorm.
3. Unzip the file, and place it in `.../%appdata%/balatro/mods` -- Make sure the Mod's directory name is 'Brainstorm' [^1]
4. Reload the game to activate the mod.

### Macos

2. Clone this repo into your Balatro Mods folder:

   ```bash
   mkdir -p ~/Library/Application\ Support/Balatro/Mods && git clone https://github.com/carterhansen467-bot/Brainstorm.git ~/Library/Application\ Support/Balatro/Mods/Brainstorm
   ```

3. Install the lovely injector into the game folder (downloads the two loader files,
   `liblovely.dylib` and `run_lovely_macos.sh`, from lovely's official releases):

   ```bash
   bash ~/Library/Application\ Support/Balatro/Mods/Brainstorm/install_lovely_macos.sh
   ```

4. **Launch the game from Terminal** — on macOS Steam's Play button does NOT load
   mods (it skips the injector), so start Balatro with:

   ```bash
   ~/Library/Application\ Support/Steam/steamapps/common/Balatro/run_lovely_macos.sh
   ```

5. To update the mod later:

   ```bash
   git -C ~/Library/Application\ Support/Balatro/Mods/Brainstorm pull
   ```

   > **Updating from an older version?** If `git pull` complains about local
   > changes to `settings.lua`, run this once (your in-game mod settings reset
   > to defaults; newer versions store settings outside of git so this never
   > happens again):
   >
   > ```bash
   > git -C ~/Library/Application\ Support/Balatro/Mods/Brainstorm checkout -- settings.lua && git -C ~/Library/Application\ Support/Balatro/Mods/Brainstorm pull
   > ```

## Features
### Save-States
Brainstorm has the capability to save up to 5 save-states through the use of in-game key binds. 
> To create a save-state: Hold `z + 1-5`
> To load a save-state:	Hold `x + 1-5`

Each number from 0 - 5 corresponds to a save slot. To overwrite an old save, simply create a new save-state in it's slot. 

### Fast Rerolling
Brainstorm allows for super-fast rerolling through the use of an in-game key bind. 
> To fast-roll:	Press `Ctrl + t`

### Auto-Rerolling
Brainstorm can automatically reroll for parameters as specified by the user.
You can edit the Auto-Reroll parameters in the Brainstorm in-game settings page.
> To Auto-Reroll:	Press `Ctrl + a`
