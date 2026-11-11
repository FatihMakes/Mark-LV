// Game-instance-lifetime owner of the active hero profile, with disk-backed preset slots.
#pragma once

#include "CoreMinimal.h"
#include "Subsystems/GameInstanceSubsystem.h"
#include "HeroTypes.h"
#include "HeroProfileSubsystem.generated.h"

UCLASS()
class HEROCORE_API UHeroProfileSubsystem : public UGameInstanceSubsystem
{
	GENERATED_BODY()

public:
	static constexpr int32 MinPresetSlot = 0;
	static constexpr int32 MaxPresetSlot = 4; // 5 preset slots, per design spec.

	// Resets the active profile to the species default kit. Rejects invalid species.
	UFUNCTION(BlueprintCallable, Category = "Hero|Profile")
	bool InitializeDefaultProfile(EHeroSpecies Species, FString& OutError);

	// Replaces the active profile after full validation (species lock + loadout shape).
	UFUNCTION(BlueprintCallable, Category = "Hero|Profile")
	bool SetProfile(const FHeroProfile& NewProfile, FString& OutError);

	UFUNCTION(BlueprintPure, Category = "Hero|Profile")
	const FHeroProfile& GetProfile() const { return CurrentProfile; }

	UFUNCTION(BlueprintCallable, Category = "Hero|Profile")
	bool SaveProfile(int32 SlotIndex, FString& OutError);

	UFUNCTION(BlueprintCallable, Category = "Hero|Profile")
	bool LoadProfile(int32 SlotIndex, FString& OutError);

	UFUNCTION(BlueprintPure, Category = "Hero|Profile")
	bool DoesPresetExist(int32 SlotIndex) const;

private:
	UPROPERTY()
	FHeroProfile CurrentProfile;

	FString MakeSlotName(int32 SlotIndex) const;
	bool IsSlotIndexValid(int32 SlotIndex, FString& OutError) const;
};
