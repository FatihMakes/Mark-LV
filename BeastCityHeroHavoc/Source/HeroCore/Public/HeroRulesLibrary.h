// Central enforcement point for the species lock and loadout validation rules.
// Every system that accepts a hero profile MUST route through here first.
#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "HeroTypes.h"
#include "HeroRulesLibrary.generated.h"

UCLASS()
class HEROCORE_API UHeroRulesLibrary : public UBlueprintFunctionLibrary
{
	GENERATED_BODY()

public:
	// Hard gate: only Raccoon, Chicken, Cat are ever valid.
	UFUNCTION(BlueprintPure, Category = "Hero|Rules")
	static bool IsValidSpecies(EHeroSpecies Species);

	// Validates loadout shape (2 actives, 1 ultimate, 2 passives) and species lock.
	UFUNCTION(BlueprintCallable, Category = "Hero|Rules")
	static bool ValidateProfile(const FHeroProfile& Profile, FString& OutError);

	// Species-specific starter kit, matching the signature 9-power design (3 per species).
	UFUNCTION(BlueprintCallable, Category = "Hero|Rules")
	static void GetDefaultPowerKit(EHeroSpecies Species, TArray<FName>& OutActives, FName& OutUltimate, TArray<FName>& OutPassives);

	// Convenience: builds a fully valid default profile for a species in one call.
	UFUNCTION(BlueprintCallable, Category = "Hero|Rules")
	static FHeroProfile MakeDefaultProfile(EHeroSpecies Species);
};
