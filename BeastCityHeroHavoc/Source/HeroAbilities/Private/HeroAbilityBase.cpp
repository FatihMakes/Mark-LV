#include "HeroAbilityBase.h"
#include "Engine/World.h"

void UHeroAbilityBase::InitializeAbility(AActor* InOwningCharacter)
{
	OwningCharacter = InOwningCharacter;
}

bool UHeroAbilityBase::TryActivate()
{
	if (IsOnCooldown())
	{
		return false;
	}

	const UWorld* World = OwningCharacter ? OwningCharacter->GetWorld() : nullptr;
	LastActivationTimeSeconds = World ? World->GetTimeSeconds() : 0.0;

	ExecuteEffect();
	return true;
}

bool UHeroAbilityBase::IsOnCooldown() const
{
	return GetRemainingCooldown() > 0.0f;
}

float UHeroAbilityBase::GetRemainingCooldown() const
{
	const UWorld* World = OwningCharacter ? OwningCharacter->GetWorld() : nullptr;
	if (!World || LastActivationTimeSeconds < 0.0)
	{
		return 0.0f;
	}

	const double Elapsed = World->GetTimeSeconds() - LastActivationTimeSeconds;
	return static_cast<float>(FMath::Max(0.0, CooldownSeconds - Elapsed));
}

void UHeroAbilityBase::ExecuteEffect_Implementation()
{
	// Base no-op. Concrete abilities override this with their real gameplay effect.
}
