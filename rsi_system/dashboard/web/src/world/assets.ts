export function sceneAssetsIncomplete(style: "office" | "meadow", loadedPets: number, totalPets: number, loadedLegacy: number, totalLegacy: number): boolean {
  return style === "office" ? loadedLegacy < totalLegacy : loadedPets < totalPets;
}
