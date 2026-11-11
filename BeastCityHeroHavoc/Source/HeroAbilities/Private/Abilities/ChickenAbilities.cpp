#include "Abilities/ChickenAbilities.h"
#include "GameFramework/Character.h"
#include "Kismet/GameplayStatics.h"

URocketFlapAbility::URocketFlapAbility()
{
	AbilityId = TEXT("RocketFlap");
	SlotType = EHeroAbilitySlotType::Active;
	CooldownSeconds = 3.0f;
	EnergyCost = 10.0f;
}

void URocketFlapAbility::ExecuteEffect_Implementation()
{
	if (ACharacter* Char = Cast<ACharacter>(OwningCharacter))
	{
		const FVector LaunchDir = Char->GetActorForwardVector() * LaunchVelocity.X + FVector::UpVector * LaunchVelocity.Z;
		Char->LaunchCharacter(LaunchDir, true, true);
	}
}

USonicSquawkAbility::USonicSquawkAbility()
{
	AbilityId = TEXT("SonicSquawk");
	SlotType = EHeroAbilitySlotType::Active;
	CooldownSeconds = 6.0f;
	EnergyCost = 15.0f;
}

void USonicSquawkAbility::ExecuteEffect_Implementation()
{
	if (!OwningCharacter)
	{
		return;
	}

	// Cone stun + shallow-structure break, scaffolded as a short-range radial pulse.
	// Replace the radius sweep with a true cone trace once the combat framework lands.
	UGameplayStatics::ApplyRadialDamage(OwningCharacter, ShatterDamage, OwningCharacter->GetActorLocation(), StunRadius, nullptr, { OwningCharacter }, OwningCharacter, OwningCharacter->GetInstigatorController(), true);
}

UEggBarrageUltimate::UEggBarrageUltimate()
{
	AbilityId = TEXT("EggBarrageUltimate");
	SlotType = EHeroAbilitySlotType::Ultimate;
	CooldownSeconds = 40.0f;
	EnergyCost = 100.0f;
}

void UEggBarrageUltimate::ExecuteEffect_Implementation()
{
	if (!OwningCharacter)
	{
		return;
	}

	// Scaffold: fan out NumEggs radial blasts in front of the chicken.
	// Replace with real projectile actors once the projectile/VFX pipeline exists.
	const FVector Origin = OwningCharacter->GetActorLocation();
	const FVector Forward = OwningCharacter->GetActorForwardVector();
	for (int32 Index = 0; Index < NumEggs; ++Index)
	{
		const float Spread = (Index - (NumEggs - 1) * 0.5f) * 15.0f;
		const FVector EggLocation = Origin + Forward.RotateAngleAxis(Spread, FVector::UpVector) * 500.0f;
		UGameplayStatics::ApplyRadialDamage(OwningCharacter, EggDamage, EggLocation, EggBlastRadius, nullptr, { OwningCharacter }, OwningCharacter, OwningCharacter->GetInstigatorController(), true);
	}
}
