// Runtime hero pawn: applies a validated FHeroProfile (appearance hookpoints + granted abilities).
#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Character.h"
#include "HeroTypes.h"
#include "HeroCharacterBase.generated.h"

class UHeroAbilityComponent;

UCLASS()
class HEROCORE_API AHeroCharacterBase : public ACharacter
{
	GENERATED_BODY()

public:
	AHeroCharacterBase();

	// Validates InProfile, caches it, grants its ability loadout, and fires BP hookpoints for appearance.
	UFUNCTION(BlueprintCallable, Category = "Hero")
	bool ApplyProfile(const FHeroProfile& InProfile);

	UFUNCTION(BlueprintPure, Category = "Hero")
	const FHeroProfile& GetProfile() const { return RuntimeProfile; }

	UFUNCTION(BlueprintPure, Category = "Hero")
	UHeroAbilityComponent* GetAbilityComponent() const { return AbilityComponent; }

protected:
	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Hero")
	FHeroProfile RuntimeProfile;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Hero")
	TObjectPtr<UHeroAbilityComponent> AbilityComponent;

	// Blueprint hookpoint: drive mesh/material swaps, outfit/accessory attachment, and aura VFX here.
	UFUNCTION(BlueprintImplementableEvent, Category = "Hero")
	void BP_OnAppearanceApplied(const FHeroAppearance& Appearance);
};
