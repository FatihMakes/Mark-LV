// Raccoon: Agile Tech Brawler. ScrapGrapple (mobility), EMPBurst (disable), JunkstormUltimate (AoE debris blast).
#pragma once

#include "CoreMinimal.h"
#include "HeroAbilityBase.h"
#include "RaccoonAbilities.generated.h"

UCLASS()
class HEROABILITIES_API UScrapGrappleAbility : public UHeroAbilityBase
{
	GENERATED_BODY()
public:
	UScrapGrappleAbility();
protected:
	UPROPERTY(EditDefaultsOnly, Category = "Ability|Raccoon")
	float GrappleLaunchStrength = 900.0f;

	virtual void ExecuteEffect_Implementation() override;
};

UCLASS()
class HEROABILITIES_API UEMPBurstAbility : public UHeroAbilityBase
{
	GENERATED_BODY()
public:
	UEMPBurstAbility();
protected:
	UPROPERTY(EditDefaultsOnly, Category = "Ability|Raccoon")
	float EMPRadius = 800.0f;

	virtual void ExecuteEffect_Implementation() override;
};

UCLASS()
class HEROABILITIES_API UJunkstormUltimate : public UHeroAbilityBase
{
	GENERATED_BODY()
public:
	UJunkstormUltimate();
protected:
	UPROPERTY(EditDefaultsOnly, Category = "Ability|Raccoon")
	float BlastRadius = 1400.0f;

	UPROPERTY(EditDefaultsOnly, Category = "Ability|Raccoon")
	float BlastDamage = 2000.0f;

	virtual void ExecuteEffect_Implementation() override;
};
