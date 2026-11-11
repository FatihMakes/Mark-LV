#include "CityHeatSubsystem.h"
#include "Engine/World.h"

void UCityHeatSubsystem::AddHeat(int32 Amount)
{
	if (Amount <= 0)
	{
		return;
	}

	const UWorld* World = GetWorld();
	LastAggressionTimeSeconds = World ? World->GetTimeSeconds() : 0.0;

	SetHeatTier(FMath::Clamp(CurrentHeatTier + Amount, MinHeatTier, MaxHeatTier));
}

void UCityHeatSubsystem::ResetHeat()
{
	SetHeatTier(MinHeatTier);
}

void UCityHeatSubsystem::SetHeatTier(int32 NewTier)
{
	NewTier = FMath::Clamp(NewTier, MinHeatTier, MaxHeatTier);
	if (NewTier == CurrentHeatTier)
	{
		return;
	}

	CurrentHeatTier = NewTier;
	OnHeatTierChanged.Broadcast(CurrentHeatTier);
}

void UCityHeatSubsystem::Tick(float DeltaTime)
{
	if (CurrentHeatTier <= MinHeatTier)
	{
		return;
	}

	const UWorld* World = GetWorld();
	const double Now = World ? World->GetTimeSeconds() : 0.0;

	// Wait for the full "no aggression" window before starting to decay.
	if (Now - LastAggressionTimeSeconds < DecayDelaySeconds)
	{
		return;
	}

	if (Now - LastDecayTimeSeconds < DecayTickSeconds)
	{
		return;
	}

	LastDecayTimeSeconds = Now;
	SetHeatTier(CurrentHeatTier - 1);
}

TStatId UCityHeatSubsystem::GetStatId() const
{
	RETURN_QUICK_DECLARE_CYCLE_STAT(UCityHeatSubsystem, STATGROUP_Tickables);
}

bool UCityHeatSubsystem::DoesSupportWorldType(EWorldType::Type WorldType) const
{
	return WorldType == EWorldType::Game || WorldType == EWorldType::PIE;
}
