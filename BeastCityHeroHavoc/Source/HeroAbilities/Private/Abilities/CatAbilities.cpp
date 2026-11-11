#include "Abilities/CatAbilities.h"
#include "GameFramework/Character.h"
#include "Engine/World.h"
#include "TimerManager.h"

UPhasePounceAbility::UPhasePounceAbility()
{
	AbilityId = TEXT("PhasePounce");
	SlotType = EHeroAbilitySlotType::Active;
	CooldownSeconds = 3.5f;
	EnergyCost = 10.0f;
}

void UPhasePounceAbility::ExecuteEffect_Implementation()
{
	if (ACharacter* Char = Cast<ACharacter>(OwningCharacter))
	{
		// Lock-on leap strike scaffold: pounces forward through light cover.
		// Replace with a real target-lock trace once the combat targeting system exists.
		Char->LaunchCharacter(Char->GetActorForwardVector() * PounceStrength, true, true);
	}
}

UReflexTimeAbility::UReflexTimeAbility()
{
	AbilityId = TEXT("ReflexTime");
	SlotType = EHeroAbilitySlotType::Active;
	CooldownSeconds = 12.0f;
	EnergyCost = 20.0f;
}

void UReflexTimeAbility::ExecuteEffect_Implementation()
{
	if (!OwningCharacter)
	{
		return;
	}

	OwningCharacter->CustomTimeDilation = SlowMoDilation;

	if (UWorld* World = OwningCharacter->GetWorld())
	{
		World->GetTimerManager().SetTimer(ReflexTimerHandle, this, &UReflexTimeAbility::RestoreTimeDilation, SlowMoDuration, false);
	}
}

void UReflexTimeAbility::RestoreTimeDilation()
{
	if (OwningCharacter)
	{
		OwningCharacter->CustomTimeDilation = 1.0f;
	}
}

UNineLivesUltimate::UNineLivesUltimate()
{
	AbilityId = TEXT("NineLivesUltimate");
	SlotType = EHeroAbilitySlotType::Ultimate;
	CooldownSeconds = 50.0f;
	EnergyCost = 100.0f;
}

void UNineLivesUltimate::ExecuteEffect_Implementation()
{
	if (!OwningCharacter)
	{
		return;
	}

	// Scaffold: temporary auto-evade + damage amp window.
	// Hook into a real combat/damage-amp component once combat resolution lands.
	UE_LOG(LogTemp, Log, TEXT("NineLivesUltimate: %s is invulnerable with amplified damage for %.1fs"), *OwningCharacter->GetName(), DurationSeconds);
}
