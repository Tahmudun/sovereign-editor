-- Synthetic animation interchange fixture, not a game-ready character.
local root = app.params.output
assert(root and root ~= "", "output directory required")
local s = Sprite(16, 16, ColorMode.INDEXED)
local palette = s.palettes[1]
palette:resize(4)
palette:setColor(0, Color{r=0,g=0,b=0,a=0})
palette:setColor(1, Color{r=40,g=45,b=58,a=255})
palette:setColor(2, Color{r=232,g=183,b=141,a=255})
palette:setColor(3, Color{r=91,g=183,b=149,a=255})
s.transparentColor = 0
s.layers[1].name = "Character"
local image = s.cels[1].image
for y=2,6 do for x=5,10 do image:drawPixel(x,y,2) end end
for y=7,11 do for x=4,11 do image:drawPixel(x,y,3) end end
for x=5,10 do image:drawPixel(x,2,1) end
for y=12,14 do image:drawPixel(5,y,1); image:drawPixel(10,y,1) end
s.frames[1].duration = 0.12
s:newFrame()
s.frames[2].duration = 0.18
s:newTag(1,2).name = "walk-test"
s:saveAs(root .. "/original.aseprite")
-- One palette correction applies consistently to both animation frames.
palette:setColor(3, Color{r=76,g=124,b=204,a=255})
s:saveAs(root .. "/edited.aseprite")
s:close()
local reopened = Sprite{fromFile=root .. "/edited.aseprite"}
assert(#reopened.frames == 2)
assert(reopened.palettes[1]:getColor(3).blue == 204)
assert(reopened.tags[1].name == "walk-test")
assert(reopened.layers[1].name == "Character")
print("Aseprite indexed edit/save/reopen: pass")
reopened:close()
