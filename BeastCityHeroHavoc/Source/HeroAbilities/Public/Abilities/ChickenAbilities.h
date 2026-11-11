// Chicken: Chaos Mobility Controller. RocketFlap (burst mobility), SonicSquawk (cone stun/break),
// EggBarrageUltimate (explosive cluster projectiles).
#pragma once

#include "CoreMinimal.h"
#include "HeroAbilityBase.h"
#include "ChickenAbilities.generated.h"

UCLASS()
class HEROABILITIES_API URocketFlapAbility : public UHeroAbilityBase
{
	GENERATED_BODY()
public:
	URocketFlapAbility();
protected:
	UPROPERTY(EditDefaultsOnly, Category = "Ability|Chicken")
	FVector LaunchVelocity = FVector(600.0f, 0.0f, 1200.0f);

	virtual void ExecuteEffect_Implementation() override;
};

UCLASS()
class HEROABILITIES_API USonicSquawkAbility : public UHeroAbilityBase
{
	GENERATED_BODY()
public:
	USonicSquawkAbility();
protected:
	UPROPERTY(EditDefaultsOnly, Category = "Ability|Chicken")
	float StunRadius = 600.0f;

	UPROPERTY(EditDefaultsOnly, Category = "Ability|Chicken")
	float ShatterDamage = 150.0f;

	virtual void ExecuteEffect_Implementation() override;
};

UCLASS()
class HEROABILITIES_API UEggBarrageUltimate : public UHeroAbilityBase
{
	GENERATED_BODY()
public:
	UEggBarrageUltimate();
protected:
	UPROPERTY(EditDefaultsOnly, Category = "Ability|Chicken")
	int32 NumEggs = 6;

	UPROPERTY(EditDefaultsOnly, Category = "Ability|Chicken")
	float EggBlastRadius = 450.0f;

	UPROPERTY(EditDefaultsOnly, Category = "Ability|Chicken")
	float EggDamage = 400.0f;

	virtual void ExecuteEffect_Implementation() override;
};
