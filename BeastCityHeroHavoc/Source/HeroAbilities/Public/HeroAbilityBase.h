// Base class for every activatable hero power (actives, ultimates, passives).
// Concrete species abilities (Raccoon/Chicken/Cat) derive from this in the Abilities/ subfolder.
#pragma once

#include "CoreMinimal.h"
#include "UObject/NoExportTypes.h"
#include "HeroAbilityBase.generated.h"

UENUM(BlueprintType)
enum class EHeroAbilitySlotType : uint8
{
	Active,
	Ultimate,
	Passive
};

UCLASS(Abstract, Blueprintable, BlueprintType)
class HEROABILITIES_API UHeroAbilityBase : public UObject
{
	GENERATED_BODY()

public:
	// Unique identifier matching the FName power ids used by FHeroPowerLoadout (e.g. "ScrapGrapple").
	UPROPERTY(EditDefaultsOnly, BlueprintReadOnly, Category = "Ability")
	FName AbilityId = NAME_None;

	UPROPERTY(EditDefaultsOnly, BlueprintReadOnly, Category = "Ability")
	EHeroAbilitySlotType SlotType = EHeroAbilitySlotType::Active;

	UPROPERTY(EditDefaultsOnly, BlueprintReadOnly, Category = "Ability")
	float CooldownSeconds = 5.0f;

	UPROPERTY(EditDefaultsOnly, BlueprintReadOnly, Category = "Ability")
	float EnergyCost = 10.0f;

	// Prepares this ability instance to run on behalf of OwningCharacter.
	UFUNCTION(BlueprintCallable, Category = "Ability")
	void InitializeAbility(AActor* InOwningCharacter);

	// Attempts to activate the ability; fails silently (returns false) if on cooldown.
	UFUNCTION(BlueprintCallable, Category = "Ability")
	bool TryActivate();

	UFUNCTION(BlueprintPure, Category = "Ability")
	bool IsOnCooldown() const;

	UFUNCTION(BlueprintPure, Category = "Ability")
	float GetRemainingCooldown() const;

protected:
	UPROPERTY(BlueprintReadOnly, Category = "Ability")
	TObjectPtr<AActor> OwningCharacter = nullptr;

	double LastActivationTimeSeconds = -1000.0;

	// Override in concrete ability classes (C++ or Blueprint) with the actual gameplay effect.
	UFUNCTION(BlueprintNativeEvent, Category = "Ability")
	void ExecuteEffect();
	virtual void ExecuteEffect_Implementation();
};
