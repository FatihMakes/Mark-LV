// Cat: Precision Predator. PhasePounce (lock-on leap strike), ReflexTime (slow-mo + crit window),
// NineLivesUltimate (temporary auto-evade + damage amp).
#pragma once

#include "CoreMinimal.h"
#include "HeroAbilityBase.h"
#include "Engine/TimerHandle.h"
#include "CatAbilities.generated.h"

UCLASS()
class HEROABILITIES_API UPhasePounceAbility : public UHeroAbilityBase
{
	GENERATED_BODY()
public:
	UPhasePounceAbility();
protected:
	UPROPERTY(EditDefaultsOnly, Category = "Ability|Cat")
	float PounceStrength = 1100.0f;

	virtual void ExecuteEffect_Implementation() override;
};

UCLASS()
class HEROABILITIES_API UReflexTimeAbility : public UHeroAbilityBase
{
	GENERATED_BODY()
public:
	UReflexTimeAbility();
protected:
	UPROPERTY(EditDefaultsOnly, Category = "Ability|Cat")
	float SlowMoDilation = 0.35f;

	UPROPERTY(EditDefaultsOnly, Category = "Ability|Cat")
	float SlowMoDuration = 2.5f;

	virtual void ExecuteEffect_Implementation() override;

private:
	FTimerHandle ReflexTimerHandle;
	void RestoreTimeDilation();
};

UCLASS()
class HEROABILITIES_API UNineLivesUltimate : public UHeroAbilityBase
{
	GENERATED_BODY()
public:
	UNineLivesUltimate();
protected:
	UPROPERTY(EditDefaultsOnly, Category = "Ability|Cat")
	float DurationSeconds = 6.0f;

	virtual void ExecuteEffect_Implementation() override;
};
