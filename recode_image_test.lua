-- 24x24 test image converted from your uploaded picture.
-- 16-colour palette + compact RLE.
-- Each 3-character token = run length (2 hex digits) + palette index (1 hex digit).

render.clear()

local startX, startY = 100, 100
local pixelSize = 16
local width, height = 24, 24

local palette = {
    {255, 255, 255},
    {253, 253, 253},
    {254, 254, 254},
    {251, 251, 248},
    {222, 192, 149},
    {108, 80, 72},
    {120, 108, 107},
    {234, 230, 218},
    {162, 145, 122},
    {91, 61, 51},
    {63, 40, 36},
    {254, 253, 250},
    {255, 255, 239},
    {140, 115, 81},
    {201, 165, 99},
    {252, 255, 255}
}

local data = table.concat({
    "0700110120100210100131600140150160170130110B001201101701301201401801902A"
    "01901801201109001101B01003601901A01901501901A01501301B0130A001401902A029"
    "03A02901701C01701B01205001301701401903A01901A02903A01501701301C050011010"
    "01801A01502A01901501902501902A029018012011050011010018039015019035019045"
    "017010011050011010018019015019015016018036025019016010011050021018039025"
    "01D02402601502901601701001204001201001701502901504401802502901702106001C"
    "01001701502A01E01701E02402D01501A01402206001701301C01401602A01D02801501D"
    "01801901501201001205001201001701402001801901A02601901801D014013011080012"
    "01B01702301401801502602D01E0140130C001101702403D01E01D034013012090011010"
    "01701402E04801D02401701001107001201001702401E01D03E01D028016014010011060"
    "01201001701901801701401801D02601D01701301701308001101001701A01702301801D"
    "01601801E01701001C01B08001101001701601302801D01E01D02E014011013012080012"
    "01002701F01501D06E0130A001201001701201701606E0180100120A0013014017016018"
    "06E016017010012050"
})

local pixel = 0
for i = 1, #data, 3 do
    local count = tonumber(string.sub(data, i, i + 1), 16)
    local colourIndex = tonumber(string.sub(data, i + 2, i + 2), 16)
    local c = palette[colourIndex + 1]

    if count and c then
        local remaining = count
        while remaining > 0 and pixel < width * height do
            local x = pixel % width
            local y = math.floor(pixel / width)
            local run = math.min(remaining, width - x)

            render.rect_filled(
                startX + x * pixelSize,
                startY + y * pixelSize,
                run * pixelSize,
                pixelSize,
                color(c[1], c[2], c[3])
            )

            pixel = pixel + run
            remaining = remaining - run
        end
    end
end
