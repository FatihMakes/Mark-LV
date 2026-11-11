#include "HeroAbilityComponent.h"
#include "HeroAbilityRegistry.h"

UHeroAbilityComponent::UHeroAbilityComponent()
{
	PrimaryComponentTick.bCanEverTick = false;
}

UHeroAbilityBase* UHeroAbilityComponent::GrantOne(FName AbilityId)
{
	if (AbilityId.IsNone())
	{
		return nullptr;
	}

	const TSubclassOf<UHeroAbilityBase> AbilityClass = UHeroAbilityRegistry::FindAbilityClass(AbilityId);
	if (!AbilityClass)
	{
		UE_LOG(LogTemp, Warning, TEXT("HeroAbilityComponent: unknown ability id '%s'"), *AbilityId.ToString());
		return nullptr;
	}

	UHeroAbilityBase* Instance = NewObject<UHeroAbilityBase>(this, AbilityClass);
	Instance->InitializeAbility(GetOwner());
	return Instance;
}

void UHeroAbilityComponent::GrantLoadout(const TArray<FName>& ActiveIds, FName UltimateId, const TArray<FName>& PassiveIds)
{
	ActiveAbilities.Reset();
	for (const FName& Id : ActiveIds)
	{
		if (UHeroAbilityBase* Ability = GrantOne(Id))
		{
			ActiveAbilities.Add(Ability);
		}
	}

	UltimateAbility = GrantOne(UltimateId);

	PassiveAbilities.Reset();
	for (const FName& Id : PassiveIds)
	{
		if (UHeroAbilityBase* Ability = GrantOne(Id))
		{
			PassiveAbilities.Add(Ability);
		}
	}
}

bool UHeroAbilityComponent::ActivateActive(int32 SlotIndex)
{
	if (!ActiveAbilities.IsValidIndex(SlotIndex) || !ActiveAbilities[SlotIndex])
	{
		return false;
	}
	return ActiveAbilities[SlotIndex]->TryActivate();
}

bool UHeroAbilityComponent::ActivateUltimate()
{
	return UltimateAbility ? UltimateAbility->TryActivate() : false;
}

float UHeroAbilityComponent::GetActiveCooldownRemaining(int32 SlotIndex) const
{
	if (!ActiveAbilities.IsValidIndex(SlotIndex) || !ActiveAbilities[SlotIndex])
	{
		return 0.0f;
	}
	return ActiveAbilities[SlotIndex]->GetRemainingCooldown();
}

float UHeroAbilityComponent::GetUltimateCooldownRemaining() const
{
	return UltimateAbility ? UltimateAbility->GetRemainingCooldown() : 0.0f;
}
