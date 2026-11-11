#include "Abilities/RaccoonAbilities.h"
#include "GameFramework/Character.h"
#include "Kismet/GameplayStatics.h"

UScrapGrappleAbility::UScrapGrappleAbility()
{
	AbilityId = TEXT("ScrapGrapple");
	SlotType = EHeroAbilitySlotType::Active;
	CooldownSeconds = 4.0f;
	EnergyCost = 10.0f;
}

void UScrapGrappleAbility::ExecuteEffect_Implementation()
{
	if (ACharacter* Char = Cast<ACharacter>(OwningCharacter))
	{
		// Pull-to-target scaffold: launches the raccoon along its forward vector.
		// Replace with a real line-trace + interact-target lookup once the world/anim systems exist.
		Char->LaunchCharacter(Char->GetActorForwardVector() * GrappleLaunchStrength, true, true);
	}
}

UEMPBurstAbility::UEMPBurstAbility()
{
	AbilityId = TEXT("EMPBurst");
	SlotType = EHeroAbilitySlotType::Active;
	CooldownSeconds = 8.0f;
	EnergyCost = 15.0f;
}

void UEMPBurstAbility::ExecuteEffect_Implementation()
{
	if (!OwningCharacter)
	{
		return;
	}

	// Scaffold: zero-damage radial pulse used to find/disable vehicles & turrets in range.
	// Hook up a "disable" interface on vehicle/turret actors for the real gameplay effect.
	UGameplayStatics::ApplyRadialDamage(OwningCharacter, 0.0f, OwningCharacter->GetActorLocation(), EMPRadius, nullptr, { OwningCharacter }, OwningCharacter, OwningCharacter->GetInstigatorController(), true);
}

UJunkstormUltimate::UJunkstormUltimate()
{
	AbilityId = TEXT("JunkstormUltimate");
	SlotType = EHeroAbilitySlotType::Ultimate;
	CooldownSeconds = 45.0f;
	EnergyCost = 100.0f;
}

void UJunkstormUltimate::ExecuteEffect_Implementation()
{
	if (!OwningCharacter)
	{
		return;
	}

	// Radial telekinetic debris blast: damages/fractures nearby destructible actors via TakeDamage.
	UGameplayStatics::ApplyRadialDamage(OwningCharacter, BlastDamage, OwningCharacter->GetActorLocation(), BlastRadius, nullptr, { OwningCharacter }, OwningCharacter, OwningCharacter->GetInstigatorController(), true);
}
